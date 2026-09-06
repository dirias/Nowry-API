from pydantic import BaseModel, ConfigDict, Field, field_validator
from .types import PyObjectId
from .mixins import SoftDeleteMixin
from typing import List, Optional
from datetime import datetime, timedelta

MAX_TAG_LENGTH: int = 40
MAX_TAGS_COUNT: int = 10


def validate_tag_list(value: Optional[List[str]]) -> Optional[List[str]]:
    """Trim, drop empties, dedupe case-insensitively, and enforce the limits.

    Module-level so `StudyCard` and `StudyCardUpdate` share one implementation:
    a card created through POST and the same card edited through PATCH must not
    be able to end up with differently-shaped tags.
    """
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


class SourceSection(BaseModel):
    """Which part of a document a generated card came from (D1, D3, D5)."""
    heading: str = Field(max_length=200)
    index: int = Field(ge=0)
    hash: str = Field(max_length=16)


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
        return validate_tag_list(value)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_reviewed: Optional[datetime] = None
    next_review: Optional[datetime] = None
    introduced_at: Optional[datetime] = None  # When this card was first served as a "new" card today
    ease_factor: float = Field(default=2.5, ge=1.3, le=2.5)  # SM-2 default ease factor
    interval: int = Field(default=1)  # days until the next review
    repetitions: int = Field(default=0)  # number of times the card has been reviewed

    # User-placed mark — an axis independent of SM-2 (ADR-010). It records that
    # the user flagged this card to come back to, which is the one thing the
    # scheduler cannot infer; it is NOT a difficulty rating, and the scheduler
    # never reads it. Written only by PUT/DELETE /study-cards/{id}/mark, never
    # by the generic PATCH. `None` means unmarked; the timestamp is what orders
    # a future cross-deck marked session (MARK-007).
    marked_at: Optional[datetime] = None

    # The book→cards link (docs/prd-book-cards.md D1). Written by the generator
    # and the save-to-deck flow only; absent from StudyCardUpdate, so the generic
    # PATCH cannot change where a card came from. A hand-written card has neither.
    source_book_id: Optional[str] = None
    source_book_title: Optional[str] = None
    source_section: Optional[SourceSection] = None

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
                "marked_at": None,
            }
        }


class StudyCardUpdate(BaseModel):
    """Exactly the fields a user may edit on their own card, and nothing else.

    An **allowlist**, and that is the whole point (DEBT-002). `PATCH
    /study-cards/{id}` previously took an untyped `dict` and `$set` it wholesale;
    MARK-001 stopped it moving the schedule by rejecting a denylist of protected
    names, but a denylist is unsafe by default — every field added to
    `StudyCard` afterwards was writable until somebody remembered to add it.
    Two fields had already needed that treatment, so the pattern had repeated
    once before it was noticed.

    With `extra="forbid"`, anything not named here is refused by validation
    before the route runs. Scheduler state (`ease_factor`, `interval`,
    `repetitions`, `next_review`, `last_reviewed`, `introduced_at`) is simply
    absent, so `POST /{id}/review` keeps sole ownership of it; `marked_at` is
    absent for the same reason, leaving `PUT/DELETE /{id}/mark` as the only
    writer (ADR-010). Identity fields (`_id`, `user_id`, `created_at`) are
    absent because they are nobody's to edit.

    Every field is optional: this is a PATCH, and the route applies
    `model_dump(exclude_unset=True)` so an untouched field is left alone rather
    than overwritten with a default.
    """

    model_config = ConfigDict(extra="forbid")

    # Shared across every card type
    title: Optional[str] = Field(default=None, max_length=100)
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    deck_id: Optional[str] = None
    card_type: Optional[str] = None

    # Quiz
    options: Optional[List[str]] = None
    correct_answer: Optional[str] = None
    explanation: Optional[str] = None

    # Visual
    diagram_code: Optional[str] = None
    diagram_type: Optional[str] = None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return validate_tag_list(value)
