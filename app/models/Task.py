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
