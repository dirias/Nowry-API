from pydantic import BaseModel, Field
from typing import Optional


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(None)
    description: Optional[str] = Field(None)
    status: Optional[str] = Field(None)
