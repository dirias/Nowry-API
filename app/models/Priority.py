from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from typing import Optional
from datetime import datetime

class Priority(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    annual_plan_id: str
    focus_area_id: Optional[str] = None
    title: str
    linked_entity_id: Optional[str] = None
    linked_entity_type: Optional[str] = None # goal, task, routine_morning, routine_afternoon, routine_evening
    description: Optional[str] = ""
    deadline: Optional[datetime] = None
    order: int = 0
    is_completed: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
