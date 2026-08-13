from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from typing import Optional, List, Dict, Any
from datetime import datetime

class QuarterReport(BaseModel, SoftDeleteMixin):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str
    annual_plan_id: str
    year: int
    quarter: int
    total_goals: int = 0
    completed_goals: int = 0
    total_milestones: int = 0
    completed_milestones: int = 0
    progress_percentage: int = 0
    goals_summary: List[Dict[str, Any]] = [] # Storing snapshot of goals at closing time
    reflections: Dict[str, Any] = {}
    routines_summary: Dict[str, Any] = {}
    knowledge_summary: Dict[str, Any] = {}
    created_at: datetime = Field(default_factory=datetime.now)
    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
