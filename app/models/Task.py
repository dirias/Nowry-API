from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from uuid import UUID
from datetime import datetime

class Task(BaseModel):
    id: UUID = Field(..., description="Unique task identifier")
    user_id: UUID = Field(..., description="ID of the user who owns the task")
    title: str = Field(..., description="Short description of the task")
    description: Optional[str] = Field(None, description="Additional details about the task")
    is_completed: bool = Field(..., description="Indicates whether the task is completed")
    priority: Literal['low', 'medium', 'high'] = Field(..., description="Importance level of the task")
    deadline: Optional[datetime] = Field(None, description="When the task should be completed")
    created_at: datetime = Field(..., description="Timestamp of task creation")
    updated_at: datetime = Field(..., description="Timestamp of last update")
    tags: List[str] = Field(default_factory=list, description="Labels or categories for filtering/sorting")
    position: int = Field(..., description="Order of task in list (for manual drag & drop sorting)")
    repeat_interval: Optional[str] = Field(None, description="Repetition setting like 'daily', 'weekly', etc.")

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "id": "d290f1ee-6c54-4b01-90e6-d701748f0851",
                "user_id": "a3f1c8b6-42cf-4c93-9025-5adfae10f0b3",
                "title": "Finish writing unit tests",
                "description": "Cover all endpoints with tests before code freeze",
                "is_completed": False,
                "priority": "high",
                "deadline": "2025-05-20T18:00:00Z",
                "created_at": "2025-05-15T08:00:00Z",
                "updated_at": "2025-05-17T10:45:00Z",
                "tags": ["testing", "backend"],
                "position": 2,
                "repeat_interval": "weekly"
            }
        }
