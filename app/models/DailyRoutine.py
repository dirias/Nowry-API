from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from typing import Optional, List, Dict, Any
from datetime import datetime

class DailyRoutineTemplate(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str
    name: str = "Default Routine"
    morning_routine: List[Dict[str, Any]] = []
    afternoon_routine: List[Dict[str, Any]] = []
    evening_routine: List[Dict[str, Any]] = []
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
