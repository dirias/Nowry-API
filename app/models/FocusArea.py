from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from typing import Optional
from datetime import datetime

class FocusArea(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    annual_plan_id: str
    name: str 
    description: Optional[str] = ""
    color: Optional[str] = "#3B82F6"
    icon: Optional[str] = "star"
    order: int  # 1, 2, or 3
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
