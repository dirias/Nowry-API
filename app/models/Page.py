# app/models/Page.py
from pydantic import BaseModel, Field
from bson import ObjectId
from .types import PyObjectId
from datetime import datetime


class Page(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    book_id: PyObjectId
    page_number: int = 1
    content: str = ""
    word_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
