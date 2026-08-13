# Comment.py
"""
Comments — user-private, text-anchored annotations attached to a resource.

Designed generically for future resource types (currently only "book"); see
`ResourceType` below and the `_RESOURCE_COLLECTIONS` registry in
`app/routers/comments.py` for the extension point.
"""
from pydantic import BaseModel, Field
from typing import Literal, Optional
from bson import ObjectId
from .types import PyObjectId
from .mixins import SoftDeleteMixin

from datetime import datetime


# Extend this Literal as new resource types come online (e.g. "deck", "sheet").
ResourceType = Literal["book"]


class CommentAnchor(BaseModel):
    """
    Text-anchor metadata used to relocate a comment's highlighted quote inside
    the resource body, even if surrounding content shifts slightly.
    """
    quote: str = Field(..., max_length=1000)          # exact selected text
    prefix: str = Field(default="", max_length=40)     # context immediately before the quote
    suffix: str = Field(default="", max_length=40)     # context immediately after the quote
    start_offset: int                                   # plain-text char offset at creation time
    end_offset: int
    block_index: Optional[int] = None                   # ordinal top-level block, narrows re-search


class Comment(BaseModel, SoftDeleteMixin):
    """MongoDB document for a single comment."""
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str  # author — sole access-control key, always server-injected, never trusted from client input
    resource_type: ResourceType
    resource_id: str
    anchor: CommentAnchor
    body: str = Field(..., min_length=1, max_length=4000)
    resolved: bool = False
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}
        arbitrary_types_allowed = True


class CommentCreate(BaseModel):
    """
    Request body for creating a comment.

    Deliberately has NO user_id field — the author is always injected
    server-side from the verified Firebase token, never accepted from the client.
    """
    resource_type: ResourceType
    resource_id: str
    anchor: CommentAnchor
    body: str = Field(..., min_length=1, max_length=4000)


class CommentUpdate(BaseModel):
    """Partial update body — only the comment text and/or resolved flag may change."""
    body: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    resolved: Optional[bool] = None


class CommentResponse(BaseModel):
    id: str
    user_id: str
    resource_type: ResourceType
    resource_id: str
    anchor: CommentAnchor
    body: str
    resolved: bool
    created_at: datetime
    updated_at: datetime
