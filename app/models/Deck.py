from typing import List, Optional, Literal, Any, Dict
from datetime import datetime
from pydantic import BaseModel, Field
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from .PublicContent import PublicMetadata


class DeckWithStats(BaseModel):
    """Deck enriched with real-time computed stats returned by GET /decks."""
    id: Optional[str] = Field(None, alias="_id")
    user_id: Optional[str] = None
    name: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    deck_type: str = "flashcard"
    status: str = "new"
    tags: Optional[List[str]] = []
    is_public: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Computed at request time
    total_cards: int = 0
    due_cards: int = 0
    new_cards: int = 0
    mastery: int = 0
    last_studied: Optional[datetime] = None
    is_due_soon: bool = False
    hours_until_due: Optional[int] = None
    voice_settings: Optional[dict] = None

    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class Deck(BaseModel, SoftDeleteMixin):
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
    deck_type: Literal["flashcard", "quiz", "visual"] = "flashcard"
    voice_settings: Optional[dict] = {
        "front": {"voice_name": None, "rate": 1.0, "pitch": 1.0},
        "back": {"voice_name": None, "rate": 1.0, "pitch": 1.0}
    }
    
    # Public Sharing
    is_public: bool = False
    published_at: Optional[datetime] = None
    public_metadata: Optional[PublicMetadata] = None

    # Fork Attribution (immutable — set when forked and preserved on publish)
    forked_from: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Attribution to the original forked deck. Contains original_id, original_title, original_author_id."
    )

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
