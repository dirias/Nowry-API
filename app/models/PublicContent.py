"""
Models for public content sharing and discovery
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Literal
from datetime import datetime
from .types import PyObjectId
from .topics import TopicValue

#: Editorial lifecycle of a curated deck (ADR-004). Only ``approved`` can ever
#: contribute to official status, and only the trusted curation operation in
#: ``PublicContentService.set_deck_curation`` may write it.
CurationStatus = Literal["draft", "in_review", "approved", "rejected"]

#: Upper bound for the editorial learning outcome, matching the bound already
#: used for the public description.
MAX_LEARNING_OUTCOME_LENGTH: int = 280


class DeckCuration(BaseModel):
    """
    Stored editorial curation metadata for a deck (`public_metadata.curation`).

    This is trusted server-side state. `reviewed_by` and `reviewed_at` are
    written by the curation operation from the authenticated reviewer and
    server clock — they are never accepted from a request body, and no
    ordinary publish path can reach this model.
    """

    model_config = ConfigDict(extra="ignore")

    status: CurationStatus = "draft"
    topic: TopicValue
    learning_outcome: str = Field(min_length=1, max_length=MAX_LEARNING_OUTCOME_LENGTH)
    rank: int = Field(ge=1)
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class PublicCuration(BaseModel):
    """Curation fields exposed to browse clients.

    Deliberately narrower than :class:`DeckCuration`: the editorial status and
    the reviewer's identity are internal and never leave the server.
    """

    topic: TopicValue
    learning_outcome: str
    rank: int


class PublicPublisher(BaseModel):
    """Display identity of whoever published a piece of public content."""

    name: str


def parse_deck_curation(deck: dict) -> Optional[DeckCuration]:
    """Read stored curation metadata off a deck document.

    Returns ``None`` when the deck carries no curation, or when the stored
    subdocument no longer satisfies the typed contract (for example a rank of
    zero written before this model existed). An unreadable subdocument must
    never be treated as an approval.
    """
    metadata = deck.get("public_metadata")
    if not isinstance(metadata, dict):
        return None

    raw = metadata.get("curation")
    if not isinstance(raw, dict):
        return None

    try:
        return DeckCuration(**raw)
    except Exception:
        return None


def is_official_deck(deck: dict, official_publisher_user_id: Optional[str]) -> bool:
    """Evaluate the official predicate from ADR-004.

    A deck is official only when it is live, public, owned by the configured
    Nowry publisher account, editorially approved, and filed under the topic it
    is actually browsed by. Publication by the official account is deliberately
    not sufficient on its own (FR-058), and an unconfigured publisher makes the
    predicate unsatisfiable rather than permissive.
    """
    if not official_publisher_user_id:
        return False
    if deck.get("is_public") is not True or deck.get("deleted_at") is not None:
        return False
    if str(deck.get("user_id")) != official_publisher_user_id:
        return False

    curation = parse_deck_curation(deck)
    if curation is None or curation.status != "approved":
        return False

    metadata = deck.get("public_metadata") or {}
    return curation.topic == metadata.get("category")


def public_curation(deck: dict) -> Optional[PublicCuration]:
    """Project the browse-visible curation fields for a deck."""
    curation = parse_deck_curation(deck)
    if curation is None:
        return None
    return PublicCuration(
        topic=curation.topic,
        learning_outcome=curation.learning_outcome,
        rank=curation.rank,
    )


class PublicMetadata(BaseModel):
    """
    Metadata for publicly shared content.
    Tracks engagement metrics and discovery information.
    """
    views: int = 0
    likes: int = 0
    forks: int = 0  # Number of times content was cloned/forked
    downloads: int = 0

    # Discovery & Categorization
    category: Optional[str] = None  # "Science", "Math", "Languages", etc.
    tags: List[str] = Field(default_factory=list)
    language: str = "en"  # Content language (en, es, fr, de, ja)
    difficulty_level: Optional[Literal["beginner", "intermediate", "advanced"]] = None

    # Quality Indicators
    average_rating: float = 0.0  # 0-5 stars
    rating_count: int = 0

    # Legal & Attribution
    license_type: str = "all_rights_reserved"  # CC-BY, CC-BY-SA, CC0, all_rights_reserved
    is_original_content: bool = True
    original_source: Optional[str] = None  # If adapted from elsewhere
    attribution: Optional[str] = None

    # Fork Attribution (immutable)
    forked_from: Optional[dict] = Field(
        default=None,
        description="Attribution to the original forked content. Contains original_id, original_title, original_author."
    )

    # Description
    description: Optional[str] = Field(default=None, max_length=280)

    # Access Control
    restricted_to: Optional[Literal["dev", "beta", "premium"]] = None  # Restrict visibility to specific user groups

    # Editorial Curation (trusted — see ADR-004)
    # Written ONLY by PublicContentService.set_deck_curation. The publish path
    # strips this key before constructing the model, so a client cannot publish
    # itself into approval.
    curation: Optional[DeckCuration] = None

    class Config:
        from_attributes = True


class ContentReport(BaseModel):
    """
    User reports for inappropriate or problematic public content.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    # What's being reported
    content_type: Literal["book", "deck", "card"]
    content_id: str
    content_title: Optional[str] = None  # Cached for reference
    
    # Who reported it
    reporter_user_id: str
    reporter_email: Optional[str] = None
    
    # Report details
    reason: Literal[
        "copyright",  # Copyright infringement
        "inappropriate",  # Offensive/NSFW content
        "spam",  # Spam or low-quality
        "misinformation",  # Factually incorrect
        "other"
    ]
    description: Optional[str] = None
    
    # Moderation
    status: Literal["pending", "under_review", "resolved", "dismissed"] = "pending"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    action_taken: Optional[str] = None  # "removed", "kept_public", "warned_creator", etc.
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class ContentFork(BaseModel):
    """
    Tracks when users fork/clone public content.
    Useful for analytics and showing popularity.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    # Original content
    original_content_type: Literal["book", "deck"]
    original_content_id: str
    original_creator_id: str
    
    # Fork details
    forked_content_id: str
    forked_by_user_id: str
    
    # Timestamps
    forked_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class ContentLike(BaseModel):
    """
    Tracks user likes/favorites for public content.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    content_type: Literal["book", "deck"]
    content_id: str
    user_id: str
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}


class ContentView(BaseModel):
    """
    Track views for analytics (optional - can be heavyweight).
    Consider using Redis or time-series DB for better performance.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    
    content_type: Literal["book", "deck"]
    content_id: str
    
    # Viewer info (optional for anonymous views)
    viewer_user_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    
    viewed_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        populate_by_name = True
        json_encoders = {PyObjectId: str}
