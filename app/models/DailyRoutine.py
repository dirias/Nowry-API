from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from typing import Optional, List, Dict, Any
from datetime import datetime

class DailyRoutineTemplate(BaseModel, SoftDeleteMixin):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str
    name: str = "Default Routine"
    morning_routine: List[Dict[str, Any]] = []
    afternoon_routine: List[Dict[str, Any]] = []
    evening_routine: List[Dict[str, Any]] = []
    # Keys: ISO date str "YYYY-MM-DD", values: list of completed item keys e.g. ["morning_0", "evening_2"]
    daily_completions: Dict[str, List[str]] = {}
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
