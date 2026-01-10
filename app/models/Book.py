# models.py
from pydantic import BaseModel, Field
from typing import List
from bson import ObjectId
from .types import PyObjectId
from typing import Optional

from datetime import datetime


class Book(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    title: Optional[str] = ""
    author: Optional[str] = ""
    user_id: Optional[str] = None  # Owner of the book
    created_at: Optional[datetime] = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    # pages: Optional[List[PyObjectId]] = [] # DEPRECATED
    page_limit: Optional[int] = 50
    tags: Optional[List[str]] = []
    summary: Optional[str] = ""
    cover_image: Optional[str] = ""
    cover_color: Optional[str] = ""
    page_size: Optional[str] = "a4"  # Page size preference (a4, letter, legal, etc.)
    full_content: Optional[str] = ""  # Single continuous document content
    auto_save_enabled: Optional[bool] = False  # Auto-save preference (default: off)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True

    def dict(self, **kwargs):
        d = super().dict(**kwargs)
        if "pages" in d and d["pages"] is not None:
            d["pages"] = [str(page) for page in d["pages"]]
        return d
