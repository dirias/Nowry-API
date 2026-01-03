from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from typing import Optional, List
from datetime import datetime

class Goal(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    focus_area_id: str
    priority_id: Optional[str] = None
    title: str
    description: Optional[str] = ""
    image_url: Optional[str] = ""
    target_date: Optional[datetime] = None
    progress: int = 0  # 0-100
    status: str = "not_started"  # not_started, in_progress, completed
    milestones: List[dict] = []  # List of {title: str, completed: bool}
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
