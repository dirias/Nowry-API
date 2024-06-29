# models.py
from pydantic import BaseModel, Field
from typing import List
from bson import ObjectId
from .types import PyObjectId
from datetime import datetime


class Book(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    title: str
    author: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    pages: List[PyObjectId] = []
    page_limit: int = 50
    tags: List[str] = []
    summary: str = ""
    cover_image: str = ""
    cover_color: str = ""

    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True
    
    def dict(self, **kwargs):
        d = super().dict(**kwargs)
        d['id'] = str(d['id'])
        d['pages'] = [str(page) for page in d['pages']]
        return d
