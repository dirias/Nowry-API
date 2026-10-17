"""PLAN-021 — task lists move from the browser to the server.

Pinned here:

  1. GET answers the user's live lists, oldest first, bounded at 200.
  2. POST trims the name and answers 409 on a name the user already has, in
     any case; blank and over-long names fail validation.
  3. PATCH and DELETE answer 404 for an invalid id or a list that is not the
     user's (the lookup is scoped to the owner and to live lists); PATCH
     answers 409 on a clash with another list and allows recasing its own.
  4. DELETE soft-deletes and clears `category` on the user's tasks only.
  5. The import is idempotent, remaps tasks from the legacy id to the list's
     id, reuses a list of the same name, and skips blank names.

Handlers are called directly with the `Depends()` parameters as kwargs and the
collections patched, as test_blocks.py does.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

from app.models.TaskList import TaskListCreate, TaskListImport, TaskListImportItem, TaskListOut, TaskListRename  # noqa: E402
from app.routers import task_lists as router  # noqa: E402

USER = "507f1f77bcf86cd799439011"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _list(name: str = "Work", legacy_id=None, **overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "user_id": USER,
        "name": name,
        "legacy_id": legacy_id,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
    }
    doc.update(overrides)
    return doc


def _collection(docs=None, found=None) -> MagicMock:
    collection = MagicMock()
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    collection.find_one = AsyncMock(return_value=found)
    collection.insert_one = AsyncMock()
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    collection.update_many = AsyncMock(return_value=MagicMock(modified_count=0))
    return collection


def _patches(lists: MagicMock, tasks: MagicMock | None = None):
    return (
        patch.object(router, "task_lists_collection", lists),
        patch.object(router, "tasks_collection", tasks or _collection()),
    )


# --- 1. GET ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_is_scoped_sorted_and_bounded(mock_firebase_user):
    docs = [_list("Work", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)), _list("Home")]
    lists = _collection(docs=docs)
    with patch.object(router, "task_lists_collection", lists):
        out = await router.list_task_lists(user=mock_firebase_user)

    assert [row["name"] for row in out] == ["Work", "Home"]
    assert out[0]["id"] == str(docs[0]["_id"])
    assert [TaskListOut(**row).name for row in out] == ["Work", "Home"]
    lists.find.assert_called_once_with({"user_id": USER, "deleted_at": None})
    lists.find.return_value.sort.assert_called_once_with("created_at", 1)
    lists.find.return_value.to_list.assert_awaited_once_with(length=200)


# --- 2. POST ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_trims_and_stores_a_live_list(mock_firebase_user):
    lists = _collection(found=None)
    with patch.object(router, "task_lists_collection", lists):
        out = await router.create_task_list(body=TaskListCreate(name="  Errands  "), user=mock_firebase_user)

    stored = lists.insert_one.call_args[0][0]
    assert stored["name"] == "Errands" and stored["user_id"] == USER
    assert stored["deleted_at"] is None and stored["legacy_id"] is None
    assert out["name"] == "Errands" and out["id"] == str(stored["_id"])


@pytest.mark.asyncio
async def test_create_refuses_a_name_the_user_already_has_in_any_case(mock_firebase_user):
    lists = _collection(found=_list("errands"))
    with patch.object(router, "task_lists_collection", lists):
        with pytest.raises(HTTPException) as exc:
            await router.create_task_list(body=TaskListCreate(name="Errands"), user=mock_firebase_user)
    assert exc.value.status_code == 409
    lists.insert_one.assert_not_called()
    query = lists.find_one.call_args[0][0]
    assert query["user_id"] == USER and query["deleted_at"] is None
    assert query["name"] == {"$regex": "^Errands$", "$options": "i"}


def test_names_are_trimmed_and_held_to_one_to_sixty():
    assert TaskListRename(name=" a ").name == "a"
    assert TaskListCreate(name="x" * 60).name == "x" * 60
    for bad in ("", "   ", "x" * 61):
        with pytest.raises(ValidationError):
            TaskListCreate(name=bad)
        with pytest.raises(ValidationError):
            TaskListRename(name=bad)
    with pytest.raises(ValidationError):
        TaskListImportItem(legacy_id="", name="Work")
    with pytest.raises(ValidationError):
        TaskListImport(lists=[{"legacy_id": f"list_{i}", "name": "n"} for i in range(51)])
    assert TaskListImportItem(legacy_id="list_1", name="   ").name == ""


# --- 3. PATCH --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_answers_404_for_an_invalid_id_or_another_users_list(mock_firebase_user):
    lists = _collection(found=None)
    with patch.object(router, "task_lists_collection", lists):
        for list_id in ("not-an-id", str(ObjectId())):
            with pytest.raises(HTTPException) as exc:
                await router.rename_task_list(id=list_id, body=TaskListRename(name="New"), user=mock_firebase_user)
            assert exc.value.status_code == 404
    # The invalid id never reached Mongo; the valid one was looked up by owner.
    lists.find_one.assert_awaited_once()
    query = lists.find_one.call_args[0][0]
    assert query["user_id"] == USER and query["deleted_at"] is None
    lists.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_rename_writes_the_name_and_refuses_a_clash(mock_firebase_user):
    doc = _list("Work")
    lists = _collection()
    lists.find_one = AsyncMock(side_effect=[doc, None])
    with patch.object(router, "task_lists_collection", lists):
        out = await router.rename_task_list(id=str(doc["_id"]), body=TaskListRename(name=" WORK "), user=mock_firebase_user)
    assert out["name"] == "WORK" and out["id"] == str(doc["_id"])
    clash_query = lists.find_one.call_args_list[1][0][0]
    assert clash_query["_id"] == {"$ne": doc["_id"]}
    assert lists.update_one.call_args[0][1]["$set"]["name"] == "WORK"

    lists = _collection()
    lists.find_one = AsyncMock(side_effect=[doc, _list("Home")])
    with patch.object(router, "task_lists_collection", lists):
        with pytest.raises(HTTPException) as exc:
            await router.rename_task_list(id=str(doc["_id"]), body=TaskListRename(name="home"), user=mock_firebase_user)
    assert exc.value.status_code == 409
    lists.update_one.assert_not_called()


# --- 4. DELETE -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_soft_deletes_and_clears_category_on_the_users_tasks_only(mock_firebase_user):
    doc = _list("Work")
    lists, tasks = _collection(found=doc), _collection()
    p_lists, p_tasks = _patches(lists, tasks)
    with p_lists, p_tasks:
        out = await router.delete_task_list(id=str(doc["_id"]), user=mock_firebase_user)

    assert out is None
    filter_, update = lists.update_one.call_args[0]
    assert filter_ == {"_id": doc["_id"]}
    assert update["$set"]["deleted_at"] is not None and update["$set"]["updated_at"] is not None
    lists.delete_one.assert_not_called()
    t_filter, t_update = tasks.update_many.call_args[0]
    assert t_filter == {"user_id": USER, "category": str(doc["_id"])}
    assert t_update["$set"]["category"] is None and "updated_at" in t_update["$set"]


@pytest.mark.asyncio
async def test_delete_answers_404_for_another_users_or_invalid_list(mock_firebase_user):
    lists, tasks = _collection(found=None), _collection()
    p_lists, p_tasks = _patches(lists, tasks)
    with p_lists, p_tasks:
        for list_id in ("bogus", str(ObjectId())):
            with pytest.raises(HTTPException) as exc:
                await router.delete_task_list(id=list_id, user=mock_firebase_user)
            assert exc.value.status_code == 404
    lists.update_one.assert_not_called()
    tasks.update_many.assert_not_called()


# --- 5. import -------------------------------------------------------------------


def _import(*pairs: tuple[str, str]) -> TaskListImport:
    return TaskListImport(lists=[{"legacy_id": legacy, "name": name} for legacy, name in pairs])


@pytest.mark.asyncio
async def test_import_creates_lists_and_remaps_tasks(mock_firebase_user):
    lists, tasks = _collection(docs=[]), _collection()
    p_lists, p_tasks = _patches(lists, tasks)
    with p_lists, p_tasks:
        await router.import_task_lists(body=_import(("list_1", " Work "), ("list_2", "Home")), user=mock_firebase_user)

    inserted = [c[0][0] for c in lists.insert_one.call_args_list]
    assert [(d["name"], d["legacy_id"]) for d in inserted] == [("Work", "list_1"), ("Home", "list_2")]
    remaps = [c[0] for c in tasks.update_many.call_args_list]
    assert remaps == [
        ({"user_id": USER, "category": "list_1"}, {"$set": {"category": str(inserted[0]["_id"])}}),
        ({"user_id": USER, "category": "list_2"}, {"$set": {"category": str(inserted[1]["_id"])}}),
    ]
    assert lists.find.return_value.to_list.await_count == 2
    lists.find.return_value.to_list.assert_awaited_with(length=200)


@pytest.mark.asyncio
async def test_import_is_idempotent(mock_firebase_user):
    work, home = _list("Work", "list_1"), _list("Home", "list_2")
    lists, tasks = _collection(docs=[work, home]), _collection()
    p_lists, p_tasks = _patches(lists, tasks)
    with p_lists, p_tasks:
        out = await router.import_task_lists(body=_import(("list_1", "Work"), ("list_2", "Home")), user=mock_firebase_user)

    lists.insert_one.assert_not_called()
    lists.update_one.assert_not_called()
    assert [row["id"] for row in out] == [str(work["_id"]), str(home["_id"])]
    # The remap still runs (it matches nothing the second time) and targets the same lists.
    targets = [c[0][1]["$set"]["category"] for c in tasks.update_many.call_args_list]
    assert targets == [str(work["_id"]), str(home["_id"])]


@pytest.mark.asyncio
async def test_import_reuses_a_list_of_the_same_name_and_keeps_its_legacy_id(mock_firebase_user):
    bare = _list("Errands")  # made on the server, never carried over
    claimed = _list("Home", "list_old")  # carried over from another browser
    lists, tasks = _collection(docs=[bare, claimed]), _collection()
    p_lists, p_tasks = _patches(lists, tasks)
    with p_lists, p_tasks:
        await router.import_task_lists(body=_import(("list_9", "errands"), ("list_8", "HOME")), user=mock_firebase_user)

    lists.insert_one.assert_not_called()
    lists.update_one.assert_awaited_once_with({"_id": bare["_id"]}, {"$set": {"legacy_id": "list_9"}})
    remaps = [c[0] for c in tasks.update_many.call_args_list]
    assert remaps == [
        ({"user_id": USER, "category": "list_9"}, {"$set": {"category": str(bare["_id"])}}),
        ({"user_id": USER, "category": "list_8"}, {"$set": {"category": str(claimed["_id"])}}),
    ]


@pytest.mark.asyncio
async def test_import_skips_blank_names_and_merges_duplicates_in_one_batch(mock_firebase_user):
    lists, tasks = _collection(docs=[]), _collection()
    p_lists, p_tasks = _patches(lists, tasks)
    with p_lists, p_tasks:
        await router.import_task_lists(
            body=_import(("list_1", "   "), ("list_2", "Work"), ("list_3", "work")), user=mock_firebase_user
        )

    assert lists.insert_one.await_count == 1
    work_id = str(lists.insert_one.call_args[0][0]["_id"])
    remaps = [c[0] for c in tasks.update_many.call_args_list]
    assert [r[0]["category"] for r in remaps] == ["list_2", "list_3"]
    assert {r[1]["$set"]["category"] for r in remaps} == {work_id}
