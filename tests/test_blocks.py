"""PLAN-001 — the Block and its log (ADR-045).

Pinned here:

  1. Occurrence expansion is pure: weekday rules, one-offs, soft-deleted
     blocks excluded, the order, and the 62-day cap.
  2. The migration's builders map routine items, completions and activities
     exactly as PRD D10 says; a user who already has blocks is never migrated.
  3. The log is one row per (owner, block, local date): the upsert's key and
     `$setOnInsert`, and the delete's scope.
  4. PATCH refuses unknown fields; another user's block is 404; the merge rule
     keeps exactly one of a weekday rule or a date.

Handlers are called directly with the `Depends()` parameters as kwargs and the
collections patched, as test_device_registry.py does.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

from app.models.Block import BlockCreate, BlockLogUpsert, BlockUpdate  # noqa: E402
from app.routers import blocks as router  # noqa: E402
from app.services import blocks as svc  # noqa: E402

USER = "507f1f77bcf86cd799439011"
OTHER = "507f1f77bcf86cd799439022"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _block(**overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "user_id": USER,
        "title": "Morning review",
        "source": {"type": "study", "id": None},
        "start": "07:30",
        "length_min": 20,
        "days_of_week": [0, 1, 2, 3, 4],
        "date": None,
        "reminder_min": 5,
        "after_block_id": None,
        "area_id": None,
        "migrated_from": None,
        "created_at": NOW,
        "updated_at": NOW,
        "deleted_at": None,
        "deleted_by": None,
    }
    doc.update(overrides)
    return doc


def _collection(docs=None, found=None) -> MagicMock:
    collection = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=list(docs or []))
    collection.find = MagicMock(return_value=cursor)
    collection.find_one = AsyncMock(return_value=found)
    collection.insert_one = AsyncMock()
    collection.insert_many = AsyncMock()
    collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    collection.delete_one = AsyncMock()
    return collection


# --- 1. expansion -------------------------------------------------------------


def test_expand_weekday_rule_one_off_and_deleted():
    weekdays = _block(title="Weekdays")
    one_off = _block(title="Dentist", days_of_week=[], date="2026-10-17", start="09:00")
    gone = _block(title="Gone", deleted_at=NOW)
    # 2026-10-12 is a Monday; the range is Mon..Sun.
    rows = svc.expand_occurrences([weekdays, one_off, gone], date(2026, 10, 12), date(2026, 10, 18))
    assert [r["date"] for r in rows if r["title"] == "Weekdays"] == [f"2026-10-{d}" for d in (12, 13, 14, 15, 16)]
    assert [r["date"] for r in rows if r["title"] == "Dentist"] == ["2026-10-17"]
    assert not [r for r in rows if r["title"] == "Gone"]
    # Ordered by day, then start.
    assert rows == sorted(rows, key=lambda r: (r["date"], r["start"], r["title"]))


def test_range_is_ordered_and_capped():
    assert svc.parse_range("2026-10-01", "2026-12-01") == (date(2026, 10, 1), date(2026, 12, 1))
    with pytest.raises(HTTPException) as too_long:
        svc.parse_range("2026-10-01", "2026-12-02")
    assert too_long.value.status_code == 400
    with pytest.raises(HTTPException):
        svc.parse_range("2026-10-02", "2026-10-01")
    with pytest.raises(HTTPException):
        svc.parse_range("yesterday", "2026-10-01")


# --- 2. the migration -----------------------------------------------------------


def test_routine_items_become_daily_blocks_in_sequence():
    routine = {
        "user_id": USER,
        "morning_routine": [{"id": "m1", "title": "Stretch"}, {"id": "m2", "text": "Read 10 pages"}, {"id": "", "title": "no id"}],
        "evening_routine": [{"id": "e1", "title": "Plan tomorrow"}],
        "daily_completions": {"2026-10-14": ["m1", "e1", "ghost"], "2026-10-15": ["m2"]},
    }
    docs, index = svc.routine_blocks(routine, NOW)
    assert [d["title"] for d in docs] == ["Stretch", "Read 10 pages", "Plan tomorrow"]
    assert [d["start"] for d in docs] == ["07:00", "07:10", "20:00"]
    assert all(d["days_of_week"] == [0, 1, 2, 3, 4, 5, 6] and d["source"]["type"] == "routine" for d in docs)
    assert [d["migrated_from"]["id"] for d in docs] == ["m1", "m2", "e1"]
    logs = svc.completion_logs(routine, docs, index, NOW)
    assert sorted((l["date"], l["status"]) for l in logs) == [("2026-10-14", "kept"), ("2026-10-14", "kept"), ("2026-10-15", "kept")]
    assert {l["block_id"] for l in logs} == {str(d["_id"]) for d in docs}


def test_activity_becomes_a_goal_block_with_its_weekdays_and_minutes():
    goal = {"_id": ObjectId(), "user_id": USER, "focus_area_id": "area-1"}
    activity = {"_id": ObjectId(), "title": "Run", "frequency": "custom", "days_of_week": [0, 2, 4], "time_of_day": "evening", "duration_minutes": 33}
    doc = svc.activity_block(activity, goal, NOW)
    assert doc["source"] == {"type": "goal", "id": str(goal["_id"])}
    assert doc["days_of_week"] == [0, 2, 4] and doc["start"] == "20:00" and doc["length_min"] == 35
    assert doc["area_id"] == "area-1" and doc["migrated_from"] == {"collection": "activities", "id": str(activity["_id"])}
    daily = svc.activity_block({**activity, "frequency": "daily", "time_of_day": "anytime"}, goal, NOW)
    assert daily["days_of_week"] == [0, 1, 2, 3, 4, 5, 6] and daily["start"] == "13:00"
    assert svc.activity_block({**activity, "is_active": False}, goal, NOW) is None


@pytest.mark.asyncio
async def test_a_user_with_blocks_is_never_migrated(mock_firebase_user):
    existing = _block()
    with patch.object(router, "blocks_collection", _collection(docs=[existing])), patch.object(
        router, "block_log_collection", _collection(docs=[])
    ), patch.object(router, "migrate_legacy", AsyncMock()) as migrate:
        out = await router.list_blocks(from_="2026-10-12", to="2026-10-18", user=mock_firebase_user)
    migrate.assert_not_called()
    assert out["migrated"] is None
    assert len(out["occurrences"]) == 5 and out["blocks"][0]["id"] == str(existing["_id"])
    # The title travels on the row, and the response model must keep it.
    from app.models.Block import BlocksResponse

    assert BlocksResponse(**out).occurrences[0].title == "Morning review"


@pytest.mark.asyncio
async def test_first_read_migrates_and_rereads(mock_firebase_user):
    migrated = _block(migrated_from={"collection": "daily_routines", "id": "m1"})
    blocks = _collection(docs=[])
    blocks.find.return_value.to_list = AsyncMock(side_effect=[[], [migrated]])
    log_row = {"_id": ObjectId(), "user_id": USER, "block_id": str(migrated["_id"]), "date": "2026-10-13", "status": "kept", "minutes": 10}
    with patch.object(router, "blocks_collection", blocks), patch.object(
        router, "block_log_collection", _collection(docs=[log_row])
    ), patch.object(router, "migrate_legacy", AsyncMock(return_value={"blocks": 1, "logs": 1})):
        out = await router.list_blocks(from_="2026-10-12", to="2026-10-18", user=mock_firebase_user)
    assert out["migrated"] == {"blocks": 1, "logs": 1}
    by_date = {o["date"]: o for o in out["occurrences"]}
    assert by_date["2026-10-13"]["log"]["status"] == "kept" and by_date["2026-10-14"]["log"] is None


# --- 3. the log -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_upsert_is_keyed_by_owner_block_and_date(mock_firebase_user):
    block = _block()
    logs = _collection(found={"_id": ObjectId(), "user_id": USER, "block_id": str(block["_id"]), "date": "2026-10-15", "status": "kept", "minutes": 18})
    body = BlockLogUpsert(status="kept", minutes=18, record={"study_session_id": "s1", "cards": 27})
    with patch.object(router, "blocks_collection", _collection(found=block)), patch.object(router, "block_log_collection", logs):
        out = await router.upsert_log(block_id=str(block["_id"]), date="2026-10-15", body=body, user=mock_firebase_user)
    key, update = logs.update_one.call_args.args
    assert key == {"user_id": USER, "block_id": str(block["_id"]), "date": "2026-10-15"}
    assert logs.update_one.call_args.kwargs["upsert"] is True
    assert update["$set"]["status"] == "kept" and update["$set"]["record"] == {"study_session_id": "s1", "cards": 27}
    assert update["$setOnInsert"]["block_id"] == str(block["_id"]) and "created_at" in update["$setOnInsert"]
    assert out["status"] == "kept"


@pytest.mark.asyncio
async def test_log_refuses_a_bad_date_and_a_foreign_block(mock_firebase_user):
    body = BlockLogUpsert(status="skipped")
    with patch.object(router, "blocks_collection", _collection(found=_block())):
        with pytest.raises(HTTPException) as bad:
            await router.upsert_log(block_id=str(ObjectId()), date="15/10/2026", body=body, user=mock_firebase_user)
    assert bad.value.status_code == 400
    with patch.object(router, "blocks_collection", _collection(found=None)):
        with pytest.raises(HTTPException) as missing:
            await router.upsert_log(block_id=str(ObjectId()), date="2026-10-15", body=body, user=mock_firebase_user)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_log_delete_is_scoped_to_the_owner(mock_firebase_user):
    logs = _collection()
    block_id = str(ObjectId())
    with patch.object(router, "block_log_collection", logs):
        await router.delete_log(block_id=block_id, date="2026-10-15", user=mock_firebase_user)
    (scope,) = logs.delete_one.call_args.args
    assert scope == {"user_id": USER, "block_id": block_id, "date": "2026-10-15"}


# --- 4. create and patch ----------------------------------------------------------


def test_create_body_needs_exactly_one_rule_and_a_five_minute_length():
    BlockCreate(title="Run", start="17:30", length_min=30, days_of_week=[0, 2, 4])
    BlockCreate(title="Dentist", start="09:00", length_min=45, date="2026-10-17")
    with pytest.raises(ValidationError):
        BlockCreate(title="Both", start="09:00", length_min=30, days_of_week=[1], date="2026-10-17")
    with pytest.raises(ValidationError):
        BlockCreate(title="Neither", start="09:00", length_min=30)
    with pytest.raises(ValidationError):
        BlockCreate(title="Odd", start="09:00", length_min=33, days_of_week=[1])
    with pytest.raises(ValidationError):
        BlockCreate(title="Late", start="24:00", length_min=30, days_of_week=[1])


def test_patch_refuses_fields_a_client_may_not_write():
    with pytest.raises(ValidationError):
        BlockUpdate(user_id=OTHER)
    with pytest.raises(ValidationError):
        BlockUpdate(migrated_from={"collection": "x", "id": "y"})
    with pytest.raises(ValidationError):
        BlockUpdate(reminder_min=7)


@pytest.mark.asyncio
async def test_patch_keeps_one_rule_and_404s_a_foreign_block(mock_firebase_user):
    block = _block()
    blocks = _collection(found=block)
    with patch.object(router, "blocks_collection", blocks):
        out = await router.update_block(block_id=str(block["_id"]), body=BlockUpdate(date="2026-10-20"), user=mock_firebase_user)
    assert out["date"] == "2026-10-20" and out["days_of_week"] == []
    _, update = blocks.update_one.call_args.args
    assert update["$set"]["days_of_week"] == [] and update["$set"]["date"] == "2026-10-20"
    with patch.object(router, "blocks_collection", _collection(found=None)):
        with pytest.raises(HTTPException) as missing:
            await router.update_block(block_id=str(block["_id"]), body=BlockUpdate(title="x"), user=mock_firebase_user)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_create_stamps_the_owner_and_never_trusts_one(mock_firebase_user):
    blocks = _collection()
    body = BlockCreate(title="  Spanish verbs ", start="20:00", length_min=10, days_of_week=[4, 0, 2, 0])
    with patch.object(router, "blocks_collection", blocks):
        out = await router.create_block(body=body, user=mock_firebase_user)
    (doc,) = blocks.insert_one.call_args.args
    assert doc["user_id"] == USER and doc["title"] == "Spanish verbs" and doc["days_of_week"] == [0, 2, 4]
    assert doc["migrated_from"] is None and doc["deleted_at"] is None and out["id"] == str(doc["_id"])


@pytest.mark.asyncio
async def test_summary_counts_planned_and_minutes_by_source_and_area(mock_firebase_user):
    study = _block(source={"type": "study", "id": None}, area_id=None)
    run = _block(title="Run", source={"type": "goal", "id": "g"}, area_id="area-1", days_of_week=[0, 2, 4], length_min=30)
    logs = [
        {"block_id": str(study["_id"]), "date": "2026-10-12", "status": "kept", "minutes": 18},
        {"block_id": str(study["_id"]), "date": "2026-10-13", "status": "skipped", "minutes": 20},
        {"block_id": str(run["_id"]), "date": "2026-10-12", "status": "partial", "minutes": 12},
    ]
    with patch.object(router, "blocks_collection", _collection(docs=[study, run])), patch.object(
        router, "block_log_collection", _collection(docs=logs)
    ):
        out = await router.block_summary(from_="2026-10-12", to="2026-10-18", user=mock_firebase_user)
    assert out["planned"] == 5 + 3 and (out["kept"], out["skipped"], out["partial"]) == (1, 1, 1)
    assert out["minutes"] == 30 and out["by_source"] == {"study": 18, "goal": 12} and out["by_area"] == {"area-1": 12}
