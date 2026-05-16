# models.py
from pydantic import BaseModel, Field
from typing import List
from bson import ObjectId
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from .PublicContent import PublicMetadata
from typing import Optional

from datetime import datetime


class Book(BaseModel, SoftDeleteMixin):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    title: Optional[str] = ""
    author: Optional[str] = ""
    user_id: Optional[str] = None  # Owner of the book
    created_at: Optional[datetime] = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    page_limit: Optional[int] = 50
    tags: Optional[List[str]] = []
    summary: Optional[str] = ""
    cover_image: Optional[str] = ""
    cover_color: Optional[str] = ""
    page_size: Optional[str] = "a4"  # Page size preference (a4, letter, legal, etc.)
    full_content: Optional[str] = ""  # Document content (JSON format for new books, HTML for legacy)
    auto_save_enabled: Optional[bool] = False  # Auto-save preference (default: off)
    illustration_count: Optional[int] = 0  # Per-book diagram generation counter (Phase 6)

    # Public Sharing
    is_public: bool = False
    published_at: Optional[datetime] = None
    public_metadata: Optional[PublicMetadata] = None

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True

class BookSummary(Book):
    """
    A lightweight version of the Book model used for lists and previews.
    It deliberately excludes the heavy `full_content` payload.
    """
    full_content: Optional[str] = Field(default=None, exclude=True)
