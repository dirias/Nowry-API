from pydantic import BaseModel, Field, field_validator
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from typing import List, Optional
from datetime import datetime, timedelta

MAX_TAG_LENGTH: int = 40
MAX_TAGS_COUNT: int = 10


class StudyCard(BaseModel, SoftDeleteMixin):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: Optional[PyObjectId] = None
    deck_id: Optional[PyObjectId] = None
    title: str = Field(..., max_length=100)
    content: str
    tags: Optional[List[str]] = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None

        trimmed_tags = [tag.strip() for tag in value]
        non_empty_tags = [tag for tag in trimmed_tags if tag]

        for tag in non_empty_tags:
            if len(tag) > MAX_TAG_LENGTH:
                raise ValueError(
                    f"Tag '{tag}' exceeds maximum length of {MAX_TAG_LENGTH} characters"
                )

        deduped_tags: List[str] = []
        seen_lowercase: set[str] = set()
        for tag in non_empty_tags:
            tag_lower = tag.lower()
            if tag_lower not in seen_lowercase:
                seen_lowercase.add(tag_lower)
                deduped_tags.append(tag)

        if len(deduped_tags) > MAX_TAGS_COUNT:
            raise ValueError(f"A card cannot have more than {MAX_TAGS_COUNT} tags")

        return deduped_tags
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_reviewed: Optional[datetime] = None
    next_review: Optional[datetime] = None
    introduced_at: Optional[datetime] = None  # When this card was first served as a "new" card today
    ease_factor: float = Field(default=2.5, ge=1.3, le=2.5)  # SM-2 default ease factor
    interval: int = Field(default=1)  # days until the next review
    repetitions: int = Field(default=0)  # number of times the card has been reviewed

    # Quiz Specific Fields
    card_type: str = Field(default="flashcard")  # "flashcard", "quiz", "visual"
    options: Optional[List[str]] = None
    correct_answer: Optional[str] = None
    explanation: Optional[str] = None

    # Visual Specific Fields
    diagram_code: Optional[str] = None
    diagram_type: Optional[str] = None

    class Config:
        from_attributes = True
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "id": 1,
                "title": "Quantum Physics Basics",
                "content": "Quantum physics is the study of matter and energy at the most fundamental level.",
                "tags": ["physics", "quantum", "science"],
                "created_at": "2024-09-02T12:00:00Z",
                "last_reviewed": "2024-09-05T12:00:00Z",
                "next_review": "2024-09-12T12:00:00Z",
                "ease_factor": 2.5,
                "interval": 7,
                "repetitions": 3,
            }
        }
