from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from datetime import datetime, timezone
from bson import ObjectId
from pymongo.collection import Collection
from app.models.Task import Task
from app.models.common import MessageResponse
from app.config.database import db
from app.utils.logger import get_logger
from app.auth.firebase_auth import get_firebase_user

from app.auth.dependencies import require_ownership

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_tasks_collection() -> Collection:
    return db.get_collection("tasks")


@router.post("/", summary="Create a new task", response_model=Task)
async def create_task(
    task: Task,
    collection: Collection = Depends(get_tasks_collection),
    user: dict = Depends(get_firebase_user),
):
    user_id = user.get("user_id")
    logger.info(f"User {user_id} creating task: {task.title}")

    task.user_id = user_id
    task.created_at = datetime.now(timezone.utc)
    task.updated_at = datetime.now(timezone.utc)

    task_dict = task.model_dump(by_alias=True, exclude={"id"})
    result = await collection.insert_one(task_dict)

    created_task = await collection.find_one({"_id": result.inserted_id})
    created_task["_id"] = str(created_task["_id"])
    if created_task.get("user_id"):
        created_task["user_id"] = str(created_task["user_id"])

    return created_task


@router.get("/", summary="List all tasks", response_model=List[Task])
async def list_tasks(
    completed: Optional[bool] = None,
    category: Optional[str] = None,
    collection: Collection = Depends(get_tasks_collection),
    user: dict = Depends(get_firebase_user),
):
    user_id = user.get("user_id")
    logger.info(f"Listing tasks for user: {user_id}")

    # Build query
    query = {"user_id": user_id}
    if completed is not None:
        query["is_completed"] = completed
    if category:
        query["category"] = category

    tasks = await collection.find(query).sort("created_at", -1).to_list(1000)

    for task in tasks:
        task["_id"] = str(task["_id"])
        if task.get("user_id"):
            task["user_id"] = str(task["user_id"])

    return tasks


@router.get("/{id}", summary="Get a task by ID", response_model=Task)
async def get_task(
    id: str,
    task: dict = Depends(require_ownership(get_tasks_collection, "id")),
):
    logger.info(f"Fetching task with ID: {id}")

    task["_id"] = str(task["_id"])
    if task.get("user_id"):
        task["user_id"] = str(task["user_id"])

    return task


@router.patch("/{id}", summary="Update a task", response_model=Task)
async def update_task(
    id: str,
    updates: dict,
    collection: Collection = Depends(get_tasks_collection),
    task: dict = Depends(require_ownership(get_tasks_collection, "id")),
    user: dict = Depends(get_firebase_user),
):
    logger.info(f"Updating task {id}")

    updates["updated_at"] = datetime.now(timezone.utc)

    # Prevent Mass Assignment of internal fields
    for field in ["_id", "id", "user_id", "created_at"]:
        updates.pop(field, None)

    # Detect task completion to award XP
    was_completed = task.get("is_completed", False)
    is_now_completed = updates.get("is_completed", was_completed)
    just_completed = (not was_completed) and is_now_completed

    # Update the task
    await collection.update_one({"_id": ObjectId(id)}, {"$set": updates})

    # Award XP when a task is marked complete for the first time
    if just_completed:
        from app.routers.agent import grant_xp
        user_id = user.get("user_id")
        await grant_xp(user_id, 50)

    # Return updated task
    updated_task = await collection.find_one({"_id": ObjectId(id)})
    updated_task["_id"] = str(updated_task["_id"])
    if updated_task.get("user_id"):
        updated_task["user_id"] = str(updated_task["user_id"])

    return updated_task



@router.delete("/{id}", summary="Delete a task", response_model=MessageResponse)
async def delete_task(
    id: str,
    collection: Collection = Depends(get_tasks_collection),
    task: dict = Depends(require_ownership(get_tasks_collection, "id")),
):
    logger.info(f"Deleting task {id}")

    await collection.delete_one({"_id": ObjectId(id)})
    return {"message": "Task deleted successfully"}
