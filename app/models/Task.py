from pydantic import BaseModel, Field
from .types import PyObjectId
from typing import Optional, List, Literal
from datetime import datetime


class Task(BaseModel):

    id: Optional[PyObjectId] = Field(alias="_id", default=None)
    user_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    is_completed: bool = False
    priority: Literal["low", "medium", "high"] = "medium"
    deadline: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    category: Optional[str] = None  # study, work, personal, etc.

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {PyObjectId: str}
        json_schema_extra = {
            "example": {
                "title": "Review Spanish vocabulary",
                "description": "Practice 50 new words",
                "is_completed": False,
                "priority": "high",
                "tags": ["study", "languages"],
                "category": "study",
            }
        }
