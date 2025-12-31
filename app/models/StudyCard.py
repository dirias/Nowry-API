from pydantic import BaseModel, Field
from .types import PyObjectId
from typing import List, Optional
from datetime import datetime, timedelta


class StudyCard(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: Optional[PyObjectId] = None
    deck_id: Optional[PyObjectId] = None
    title: str = Field(..., max_length=100)
    content: str
    tags: Optional[List[str]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_reviewed: Optional[datetime] = None
    next_review: Optional[datetime] = None
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
