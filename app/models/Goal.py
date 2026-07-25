from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from typing import Optional, List
from datetime import datetime, timezone

class Milestone(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    title: str
    due_date: Optional[str] = None
    completed: bool = False
    is_key_result: bool = False

class Goal(BaseModel, SoftDeleteMixin):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    focus_area_id: str
    priority_id: Optional[str] = None
    title: str
    description: Optional[str] = ""
    image_url: Optional[str] = ""
    target_date: Optional[datetime] = None
    parent_id: Optional[str] = None
    quarter: Optional[int] = None
    year: Optional[int] = None
    type: str = "quarterly"
    progress: int = 0
    status: str = "not_started"
    milestones: List[Milestone] = []
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    migration_count: int = 0

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
