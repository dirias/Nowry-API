from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from typing import Optional
from datetime import datetime

class AnnualPlan(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str
    year: int
    title: Optional[str] = "My Year"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
