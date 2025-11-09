from fastapi import APIRouter, Depends, HTTPException, status, Body
from bson import ObjectId
from pymongo.collection import Collection
from datetime import datetime
from uuid import uuid4, UUID
from app.models.Task import Task
from app.config.database import tasks_collection
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_tasks_collection() -> Collection:
    return tasks_collection


@router.post(
    "/",
    summary="Create a new task",
    response_model=Task,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(task: Task, collection: Collection = Depends(get_tasks_collection)):
    logger.info(f"Creating new task: {task.title}")

    task.id = uuid4()
    task.created_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()

    if not task.title or len(task.title.strip()) == 0:
        raise HTTPException(status_code=400, detail="Title is required")

    task_dict = task.dict(by_alias=True)
    result = await collection.insert_one(task_dict)

    if not result.inserted_id:
        raise HTTPException(status_code=500, detail="Failed to insert task")

    logger.info(f"Task created successfully with ID: {task.id}")
    return task

@router.delete(
    "/{id}",
    summary="Soft delete a task",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def soft_delete_task(id: str, collection: Collection = Depends(get_tasks_collection)):
    logger.info(f"Attempting to soft delete task with ID: {id}")

    try:
        task_id = UUID(id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid task ID format")

    task = await collection.find_one({"id": str(task_id)})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("deleted_at") is not None:
        raise HTTPException(status_code=409, detail="Task already soft-deleted")

    deleted_at = datetime.utcnow()
    result = await collection.update_one(
        {"id": str(task_id)},
        {"$set": {"deleted_at": deleted_at, "updated_at": deleted_at}},
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to soft delete task")

    logger.info(f"Task {id} marked as deleted at {deleted_at.isoformat()}")
    return None

@router.patch("/{id}", summary="Update an existing task", response_model=Task)
async def update_task(
    id: str,
    data: dict = Body(...),
    collection: Collection = Depends(get_tasks_collection)
):
    """
    Update one or more fields of an existing task.
    - Only updates tasks where deleted_at is null.
    - Refreshes updated_at timestamp automatically.
    """
    logger.info(f"Updating task {id}")

    if not ObjectId.is_valid(id):
        raise HTTPException(status_code=400, detail="Invalid task ID format")

    existing_task = await collection.find_one({"_id": ObjectId(id), "deleted_at": None})
    if not existing_task:
        raise HTTPException(status_code=404, detail="Task not found or soft-deleted")

    allowed_fields = {"title", "description", "status"}
    update_data = {k: v for k, v in data.items() if k in allowed_fields}

    if not update_data:
        raise HTTPException(status_code=400, detail="No valid fields provided for update")

    update_data["updated_at"] = datetime.utcnow()

    result = await collection.update_one(
        {"_id": ObjectId(id)},
        {"$set": update_data}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=400, detail="No changes were applied")

    updated_task = await collection.find_one({"_id": ObjectId(id)})

    logger.info(f"Task {id} updated successfully")
    return updated_task
