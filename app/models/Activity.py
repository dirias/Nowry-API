from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from typing import Optional
from datetime import datetime

class Activity(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    goal_id: str
    title: str
    description: Optional[str] = ""
    frequency: str = "daily"  # daily, weekly, custom
    days_of_week: Optional[list[int]] = []  # 0=Monday, 6=Sunday
    time_of_day: Optional[str] = "anytime"  # morning, afternoon, evening, anytime
    duration_minutes: Optional[int] = 30
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
