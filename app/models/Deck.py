from typing import List, Optional, Literal
from datetime import datetime
from pydantic import BaseModel, Field
from .types import PyObjectId


class Deck(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: Optional[PyObjectId] = None
    name: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    total_cards: int = 0
    status: Literal["new", "review", "attention", "archived"] = "new"
    tags: Optional[List[str]] = []
    cards: List[PyObjectId] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    deleted_at: Optional[datetime] = None
    deck_type: Literal["flashcard", "quiz", "visual"] = "flashcard"
    voice_settings: Optional[dict] = {
        "front": {"voice_name": None, "rate": 1.0, "pitch": 1.0},
        "back": {"voice_name": None, "rate": 1.0, "pitch": 1.0}
    }

    class Config:
        from_attributes = True
        populate_by_name = True
        json_encoders = {PyObjectId: str}
        json_schema_extra = {
            "example": {
                "_id": "64f7b0a9f2a1e3c8b1234567",
                "user_id": "64f7b0a9f2a1e3c8b7654321",
                "name": "Japonés",
                "description": "Mazo para estudiar vocabulario japonés.",
                "image_url": "https://example.com/images/japanese_flag.png",
                "total_cards": 50,
                "status": "new",
                "tags": ["idioma", "asia"],
                "cards": ["64f7b0a9f2a1e3c8b1111111", "64f7b0a9f2a1e3c8b2222222"],
                "created_at": "2025-10-30T10:00:00Z",
                "updated_at": "2025-10-31T10:00:00Z",
                "deleted_at": None,
            }
        }
