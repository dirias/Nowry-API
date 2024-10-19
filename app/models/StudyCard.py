from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timedelta


class StudyCard(BaseModel):
    id: int
    title: str = Field(..., max_length=100)
    content: str
    tags: Optional[List[str]] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_reviewed: Optional[datetime] = None
    next_review: Optional[datetime] = None
    ease_factor: float = Field(default=2.5, ge=1.3, le=2.5)  # SM-2 default ease factor
    interval: int = Field(default=1)  # days until the next review
    repetitions: int = Field(default=0)  # number of times the card has been reviewed

    class Config:
        orm_mode = True
        schema_extra = {
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
