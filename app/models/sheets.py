"""
Pydantic v2 models for Micro Sheets.

Sheet     — full document model (includes data payload); used for GET /{id}, POST, PUT
SheetSummary — lightweight list model (excludes data); used for GET /sheets list
SheetCreate  — request model for POST /sheets
SheetUpdate  — request model for PUT /sheets/{id}
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from bson import ObjectId
from pydantic import BaseModel, Field

from app.models.mixins import SoftDeleteMixin
from app.models.types import PyObjectId


class Sheet(BaseModel, SoftDeleteMixin):
    """Full sheet document — includes the data payload."""

    id: Optional[PyObjectId] = Field(default=None, alias="_id")
    user_id: Optional[str] = None  # Set by router from Firebase token — never from request body
    title: str = "Untitled Sheet"
    data: List[List[Any]] = []          # react-spreadsheet Matrix: array of cell object rows
    column_labels: List[str] = []       # Custom column header labels
    created_at: Optional[datetime] = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


class SheetSummary(BaseModel):
    """Lightweight model for list view — excludes the data payload for performance."""

    id: Optional[PyObjectId] = Field(default=None, alias="_id")
    user_id: Optional[str] = None
    title: str = "Untitled Sheet"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {ObjectId: str}


class SheetCreate(BaseModel):
    """Request model for POST /sheets. No id or user_id — both are set server-side."""

    title: str = "Untitled Sheet"
    data: List[List[Any]] = []
    column_labels: List[str] = []


class SheetUpdate(BaseModel):
    """Request model for PUT /sheets/{id}. All fields optional — partial update."""

    title: Optional[str] = None
    data: Optional[List[List[Any]]] = None
    column_labels: Optional[List[str]] = None
