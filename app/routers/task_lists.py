"""`/task-lists` — a task's list, named and owned on the server (PLAN-021).

A task's `category` holds its list's id. Every route injects the Firebase user;
every read is bounded; a list is reachable only through its owner. Deleting a
list soft-deletes it and leaves its tasks list-less. The import carries the
browser's localStorage lists over once and is safe to run again.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.auth.firebase_auth import get_firebase_user
from app.config.database import task_lists_collection, tasks_collection
from app.models.TaskList import TaskListCreate, TaskListImport, TaskListImportItem, TaskListOut, TaskListRename
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/task-lists",
    tags=["task-lists"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)
logger = get_logger(__name__)

MAX_LISTS = 200
NAME_TAKEN = "A list with that name already exists"


def _list_out(doc: dict) -> dict:
    return {"id": str(doc["_id"]), "name": doc["name"], "created_at": doc["created_at"], "updated_at": doc["updated_at"]}


def _oid(list_id: str) -> ObjectId:
    if not ObjectId.is_valid(list_id):
        raise HTTPException(status_code=404, detail="Task list not found")
    return ObjectId(list_id)


def _name_key(name: str) -> str:
    return name.strip().casefold()


async def _active_lists(user_id: str) -> List[dict]:
    cursor = task_lists_collection.find({"user_id": user_id, "deleted_at": None}).sort("created_at", 1)
    return await cursor.to_list(length=MAX_LISTS)


async def _owned_list(user_id: str, list_id: str) -> dict:
    doc = await task_lists_collection.find_one({"_id": _oid(list_id), "user_id": user_id, "deleted_at": None})
    if not doc:
        raise HTTPException(status_code=404, detail="Task list not found")
    return doc


async def _refuse_taken_name(user_id: str, name: str, except_id: Optional[ObjectId] = None) -> None:
    """409 when another of the user's live lists has this name, ignoring case."""
    query: dict = {
        "user_id": user_id,
        "deleted_at": None,
        "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
    }
    if except_id is not None:
        query["_id"] = {"$ne": except_id}
    if await task_lists_collection.find_one(query):
        raise HTTPException(status_code=409, detail=NAME_TAKEN)


def _new_list(user_id: str, name: str, legacy_id: Optional[str], now: datetime) -> dict:
    return {
        "_id": ObjectId(),
        "user_id": user_id,
        "name": name,
        "legacy_id": legacy_id,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }


@router.get("/", summary="The user's task lists, oldest first", response_model=List[TaskListOut])
async def list_task_lists(user: dict = Depends(get_firebase_user)) -> List[dict]:
    return [_list_out(doc) for doc in await _active_lists(user.get("user_id"))]


@router.post("/", summary="Create a task list", response_model=TaskListOut, status_code=201)
async def create_task_list(body: TaskListCreate, user: dict = Depends(get_firebase_user)) -> dict:
    user_id = user.get("user_id")
    await _refuse_taken_name(user_id, body.name)
    doc = _new_list(user_id, body.name, None, datetime.now(timezone.utc))
    await task_lists_collection.insert_one(doc)
    logger.info(f"User {user_id} created task list {doc['_id']}")
    return _list_out(doc)


@router.patch("/{id}", summary="Rename a task list", response_model=TaskListOut)
async def rename_task_list(id: str, body: TaskListRename, user: dict = Depends(get_firebase_user)) -> dict:
    user_id = user.get("user_id")
    doc = await _owned_list(user_id, id)
    await _refuse_taken_name(user_id, body.name, except_id=doc["_id"])
    now = datetime.now(timezone.utc)
    await task_lists_collection.update_one({"_id": doc["_id"]}, {"$set": {"name": body.name, "updated_at": now}})
    return _list_out({**doc, "name": body.name, "updated_at": now})


@router.delete("/{id}", summary="Delete a task list; its tasks keep living, list-less", status_code=204)
async def delete_task_list(id: str, user: dict = Depends(get_firebase_user)) -> None:
    user_id = user.get("user_id")
    doc = await _owned_list(user_id, id)
    now = datetime.now(timezone.utc)
    await task_lists_collection.update_one(
        {"_id": doc["_id"]}, {"$set": {"deleted_at": now, "deleted_by": user_id, "updated_at": now}}
    )
    await tasks_collection.update_many(
        {"user_id": user_id, "category": str(doc["_id"])}, {"$set": {"category": None, "updated_at": now}}
    )
    logger.info(f"User {user_id} deleted task list {doc['_id']}")
    return None


async def _import_one(
    user_id: str, item: TaskListImportItem, by_legacy: dict, by_name: dict, now: datetime
) -> Optional[dict]:
    """The list this browser list becomes: one already carried over, one of the
    same name, or a new one. None when the name is blank."""
    if not item.name:
        return None
    found = by_legacy.get(item.legacy_id) or by_name.get(_name_key(item.name))
    if found is None:
        found = _new_list(user_id, item.name, item.legacy_id, now)
        await task_lists_collection.insert_one(found)
    elif not found.get("legacy_id"):
        found["legacy_id"] = item.legacy_id
        await task_lists_collection.update_one({"_id": found["_id"]}, {"$set": {"legacy_id": item.legacy_id}})
    by_legacy.setdefault(item.legacy_id, found)
    by_name.setdefault(_name_key(found["name"]), found)
    return found


@router.post("/import", summary="Carry the browser's lists over, once", response_model=List[TaskListOut])
async def import_task_lists(body: TaskListImport, user: dict = Depends(get_firebase_user)) -> List[dict]:
    user_id = user.get("user_id")
    existing = await _active_lists(user_id)
    by_legacy = {doc["legacy_id"]: doc for doc in existing if doc.get("legacy_id")}
    by_name = {_name_key(doc["name"]): doc for doc in existing}
    now = datetime.now(timezone.utc)
    for item in body.lists:
        target = await _import_one(user_id, item, by_legacy, by_name, now)
        if target is None or item.legacy_id == str(target["_id"]):
            continue
        await tasks_collection.update_many(
            {"user_id": user_id, "category": item.legacy_id}, {"$set": {"category": str(target["_id"])}}
        )
    logger.info(f"User {user_id} imported {len(body.lists)} task lists")
    return [_list_out(doc) for doc in await _active_lists(user_id)]
