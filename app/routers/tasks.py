from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.collection import Collection
from datetime import datetime
from uuid import uuid4
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


@router.post("/", summary="Create a new task", response_model=Task, status_code=status.HTTP_201_CREATED)
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
