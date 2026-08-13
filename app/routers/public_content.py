"""
Public Content API Router
Handles browse, publish, fork, and engagement for public Books and Decks
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Any, Dict, Optional, List, Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from bson import ObjectId

from app.auth.dependencies import require_admin
from app.config.database import db
from app.models.PublicContent import (
    MAX_LEARNING_OUTCOME_LENGTH,
    CurationStatus,
    PublicCuration,
    PublicPublisher,
)
from app.models.topics import TOPIC_TAXONOMY, TopicValue
from app.services.public_content_service import PublicContentService
from app.auth.firebase_auth import get_current_user, optional_auth
from app.config.database import cards_collection, decks_collection

router = APIRouter(prefix="/public", tags=["Public Content"])

# Initialize service
def get_public_service() -> PublicContentService:
    return PublicContentService(db)


# ========== Request/Response Models ==========

class PublishRequest(BaseModel):
    category: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    language: str = "en"
    difficulty_level: Optional[Literal["beginner", "intermediate", "advanced"]] = None
    license_type: str = "all_rights_reserved"
    is_original_content: bool = True
    original_source: Optional[str] = None
    attribution: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=280)


class BrowseFilters(BaseModel):
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    language: Optional[str] = None
    difficulty: Optional[Literal["beginner", "intermediate", "advanced"]] = None
    search: Optional[str] = None
    sort_by: Literal["recent", "popular", "top_rated", "curated"] = "recent"


class PublicDeckBrowseItem(BaseModel):
    """One deck in the public browse envelope.

    The curated contract fields are typed; every other stored deck field passes
    through unchanged so existing browse consumers keep the shape they already
    read. `public_metadata` stays a passthrough mapping deliberately — legacy
    documents contain values outside today's literals, and a strict model would
    fail the whole page rather than one field.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str = Field(alias="_id")
    name: Optional[str] = None
    description: Optional[str] = None
    total_cards: Optional[int] = None
    public_metadata: Optional[Dict[str, Any]] = None

    # Server-derived — never read from the stored document (ADR-004).
    is_official: bool = False
    curation: Optional[PublicCuration] = None
    publisher: Optional[PublicPublisher] = None


class PublicDeckBrowsePage(BaseModel):
    """Existing page envelope, now with typed items."""

    items: List[PublicDeckBrowseItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class DeckCurationRequest(BaseModel):
    """Editorial curation payload.

    `extra="forbid"` is load-bearing: an attempt to smuggle `reviewed_by`,
    `reviewed_at` or `is_official` is rejected loudly instead of being silently
    dropped. Those values are server-derived without exception.
    """

    model_config = ConfigDict(extra="forbid")

    status: CurationStatus
    topic: TopicValue
    learning_outcome: str = Field(min_length=1, max_length=MAX_LEARNING_OUTCOME_LENGTH)
    rank: int = Field(ge=1)


class DeckCurationResponse(BaseModel):
    deck_id: str
    status: CurationStatus
    topic: TopicValue
    learning_outcome: str
    rank: int
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    is_official: bool


def _validate_curated_browse(official: bool, sort_by: str, category: Optional[str]) -> None:
    """Reject invalid official/sort/category combinations with a 400.

    Distinct from an uncovered topic: a *valid* taxonomy topic with no approved
    decks is a successful empty page (FR-059), while a nonsensical query is a
    client error.
    """
    if sort_by == "curated" and not official:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "curated_sort_requires_official",
                "message": "sort_by=curated is only valid with official=true",
            },
        )

    if official and category is not None and category not in TOPIC_TAXONOMY:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_official_category",
                "message": "official browse requires a canonical taxonomy topic",
            },
        )


class PublicCardPreview(BaseModel):
    id: str = Field(alias="_id")
    title: Optional[str] = None
    content: Optional[str] = None
    card_type: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class PublicDeckCardsResponse(BaseModel):
    cards: List[PublicCardPreview]
    total: int


# ========== Browse & Discovery (No Auth Required) ==========

@router.get("/books")
async def browse_public_books(
    category: Optional[str] = None,
    tags: Optional[str] = None,  # Comma-separated
    language: Optional[str] = None,
    difficulty: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "recent",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    request: Request = None,
    current_user: Optional[dict] = Depends(optional_auth),  # Add this dependency
    service: PublicContentService = Depends(get_public_service)
):
    """
    Browse public books.
    No authentication required, but logged-in users see more content based on their role.
    
    Query Parameters:
    - category: Filter by category (e.g., "Science", "Math")
    - tags: Comma-separated tags (e.g., "physics,quantum")
    - language: Filter by language code (e.g., "en", "es")
    - difficulty: "beginner", "intermediate", or "advanced"
    - search: Search query (searches title, summary, tags)
    - sort_by: "recent", "popular", or "top_rated"
    - page: Page number (default: 1)
    - page_size: Items per page (default: 20, max: 100)
    """
    from app.config.database import users_collection
    from bson import ObjectId
    
    # Try to get current user (optional)
    viewer_role = None
    viewer_is_beta = False
    try:
        if current_user:  # optional_auth already handled the token
            user_id = current_user.get("user_id")
            user = await users_collection.find_one({"_id": ObjectId(user_id)})
            if user:
                viewer_role = user.get("role", "user")
                viewer_is_beta = user.get("is_beta", False)
    except:
        pass  # Continue as anonymous user
    
    tag_list = tags.split(",") if tags else None
    
    result = await service.browse_public_content(
        content_type="book",
        category=category,
        tags=tag_list,
        language=language,
        difficulty=difficulty,
        search_query=search,
        sort_by=sort_by,
        page=page,
        page_size=page_size,
        viewer_role=viewer_role,
        viewer_is_beta=viewer_is_beta
    )
    
    return result


@router.get("/decks", response_model=PublicDeckBrowsePage)
async def browse_public_decks(
    category: Optional[str] = None,
    tags: Optional[str] = None,
    language: Optional[str] = None,
    difficulty: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "recent",
    official: bool = Query(
        False,
        description="Restrict to editorially approved decks from the official Nowry account",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    request: Request = None,
    current_user: Optional[dict] = Depends(optional_auth),  # Add this dependency
    service: PublicContentService = Depends(get_public_service)
) -> PublicDeckBrowsePage:
    """
    Browse public decks.
    No authentication required, but logged-in users see more content based on their role.

    Query Parameters (curated discovery, ADR-004):
    - official: when true, return only approved decks owned by the configured
      official Nowry account. `is_official` is always server-derived.
    - sort_by: "recent", "popular", "top_rated", or "curated". "curated" is
      valid only with official=true and orders by ascending editorial rank then
      ascending deck id — popularity is never consulted.
    - category: for official browse this is the canonical taxonomy topic.

    A valid topic with no approved decks returns an empty page, not an error.
    """
    from app.config.database import users_collection
    from bson import ObjectId

    _validate_curated_browse(official, sort_by, category)

    # Try to get current user (optional)
    viewer_role = None
    viewer_is_beta = False
    try:
        if current_user:  # optional_auth already handled the token
            user_id = current_user.get("user_id")
            user = await users_collection.find_one({"_id": ObjectId(user_id)})
            if user:
                viewer_role = user.get("role", "user")
                viewer_is_beta = user.get("is_beta", False)
    except:
        pass  # Continue as anonymous user

    tag_list = tags.split(",") if tags else None

    result = await service.browse_public_content(
        content_type="deck",
        category=category,
        tags=tag_list,
        language=language,
        difficulty=difficulty,
        search_query=search,
        sort_by=sort_by,
        page=page,
        page_size=page_size,
        viewer_role=viewer_role,
        viewer_is_beta=viewer_is_beta,
        official=official,
    )

    return PublicDeckBrowsePage(**result)


@router.get("/books/{book_id}")
async def get_public_book(
    book_id: str,
    request: Request,
    current_user: Optional[dict] = Depends(optional_auth),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Get a single public book.
    Tracks view for analytics.
    No authentication required, but access may be restricted based on user role.
    """
    from app.config.database import users_collection
    from bson import ObjectId
    
    viewer_id = current_user.get("user_id") if current_user else None
    viewer_role = None
    viewer_is_beta = False
    
    if current_user:
        user_id = current_user.get("user_id")
        user = await users_collection.find_one({"_id": ObjectId(user_id)})
        if user:
            viewer_role = user.get("role", "user")
            viewer_is_beta = user.get("is_beta", False)
    
    book = await service.get_public_content_by_id(
        content_type="book",
        content_id=book_id,
        viewer_user_id=viewer_id,
        viewer_role=viewer_role,
        viewer_is_beta=viewer_is_beta,
        track_view=True
    )
    
    return book


@router.get("/decks/{deck_id}")
async def get_public_deck(
    deck_id: str,
    request: Request,
    current_user: Optional[dict] = Depends(optional_auth),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Get a single public deck.
    Tracks view for analytics.
    No authentication required, but access may be restricted based on user role.
    """
    from app.config.database import users_collection
    from bson import ObjectId
    
    viewer_id = current_user.get("user_id") if current_user else None
    viewer_role = None
    viewer_is_beta = False
    
    if current_user:
        user_id = current_user.get("user_id")
        user = await users_collection.find_one({"_id": ObjectId(user_id)})
        if user:
            viewer_role = user.get("role", "user")
            viewer_is_beta = user.get("is_beta", False)
    
    deck = await service.get_public_content_by_id(
        content_type="deck",
        content_id=deck_id,
        viewer_user_id=viewer_id,
        viewer_role=viewer_role,
        viewer_is_beta=viewer_is_beta,
        track_view=True
    )

    return deck


@router.get(
    "/decks/{deck_id}/cards",
    response_model=PublicDeckCardsResponse,
    summary="Get a preview of cards from a public deck",
)
async def get_public_deck_cards(
    deck_id: str,
    limit: int = Query(default=6, ge=1, le=10),
    current_user: Optional[dict] = Depends(optional_auth),
) -> PublicDeckCardsResponse:
    """
    Return a limited card preview for a public deck.
    No authentication required.
    Only decks with is_public=True are accessible.
    SRS fields (interval, ease_factor, next_review, etc.) are never exposed.
    """
    # Resolve deck ObjectId
    try:
        deck_oid = ObjectId(deck_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Deck not found")

    deck = await decks_collection.find_one({
        "_id": deck_oid,
        "is_public": True,
        "deleted_at": None,
    })

    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found or not public")

    # Fetch cards — tolerate ObjectId or string deck_id (mixed legacy data)
    raw_cards = await cards_collection.find(
        {
            "deck_id": {"$in": [deck_oid, deck_id]},
            "deleted_at": None,
        }
    ).to_list(length=limit)

    previews: List[PublicCardPreview] = []
    for card in raw_cards:
        previews.append(
            PublicCardPreview(
                _id=str(card["_id"]),
                title=card.get("title"),
                content=card.get("content"),
                card_type=card.get("card_type"),
                tags=card.get("tags") or [],
            )
        )

    return PublicDeckCardsResponse(cards=previews, total=len(previews))


# ========== Publishing (Auth Required) ==========

@router.post("/books/{book_id}/publish")
async def publish_book(
    book_id: str,
    publish_data: PublishRequest,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Make a book public.
    Requires authentication and ownership.
    """
    result = await service.publish_content(
        content_type="book",
        content_id=book_id,
        user_id=current_user["user_id"],
        public_metadata=publish_data.model_dump()
    )

    return {
        "message": "Book published successfully",
        "book": result
    }


@router.post("/decks/{deck_id}/publish")
async def publish_deck(
    deck_id: str,
    publish_data: PublishRequest,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Make a deck public.
    Requires authentication and ownership.
    """
    result = await service.publish_content(
        content_type="deck",
        content_id=deck_id,
        user_id=current_user["user_id"],
        public_metadata=publish_data.model_dump()
    )

    return {
        "message": "Deck published successfully",
        "deck": result
    }


@router.post("/books/{book_id}/unpublish")
async def unpublish_book(
    book_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """Make a book private again"""
    result = await service.unpublish_content(
        content_type="book",
        content_id=book_id,
        user_id=current_user["user_id"]
    )
    
    return {
        "message": "Book unpublished successfully",
        "book": result
    }


@router.post("/decks/{deck_id}/unpublish")
async def unpublish_deck(
    deck_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """Make a deck private again"""
    result = await service.unpublish_content(
        content_type="deck",
        content_id=deck_id,
        user_id=current_user["user_id"]
    )
    
    return {
        "message": "Deck unpublished successfully",
        "deck": result
    }


# ========== Editorial Curation (Admin Only) ==========

@router.put(
    "/decks/{deck_id}/curation",
    response_model=DeckCurationResponse,
    summary="Set editorial curation metadata for an official deck",
)
async def set_deck_curation(
    deck_id: str,
    data: DeckCurationRequest,
    current_user: dict = Depends(require_admin),
    service: PublicContentService = Depends(get_public_service),
) -> DeckCurationResponse:
    """
    Trusted editorial operation — the ONLY writer of `public_metadata.curation`.

    Admin-gated (`is_admin=true`) and restricted to decks owned by the
    configured official publisher account. The reviewer identity and review time
    are taken from the authenticated caller and the server clock; the request
    body cannot carry them.

    Errors: `400` topic/category mismatch, unpublished deck, or an empty deck on
    approval; `403` non-admin caller or a deck outside the official account;
    `404` unknown deck; `409` the approved rank is already taken for that topic;
    `503` no official publisher configured.
    """
    result = await service.set_deck_curation(
        deck_id=deck_id,
        curation=data.model_dump(),
        reviewer_id=current_user["user_id"],
    )

    return DeckCurationResponse(**result)


# ========== Engagement (Auth Required) ==========

@router.post("/books/{book_id}/like")
async def like_book(
    book_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """Like a public book"""
    result = await service.like_content(
        content_type="book",
        content_id=book_id,
        user_id=current_user["user_id"]
    )
    
    return result


@router.delete("/books/{book_id}/like")
async def unlike_book(
    book_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """Remove like from a public book"""
    result = await service.unlike_content(
        content_type="book",
        content_id=book_id,
        user_id=current_user["user_id"]
    )
    
    return result


@router.post("/decks/{deck_id}/like")
async def like_deck(
    deck_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """Like a public deck"""
    result = await service.like_content(
        content_type="deck",
        content_id=deck_id,
        user_id=current_user["user_id"]
    )
    
    return result


@router.delete("/decks/{deck_id}/like")
async def unlike_deck(
    deck_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """Remove like from a public deck"""
    result = await service.unlike_content(
        content_type="deck",
        content_id=deck_id,
        user_id=current_user["user_id"]
    )
    
    return result


# ========== Forking/Cloning (Auth Required) ==========

@router.post("/books/{book_id}/fork")
async def fork_book(
    book_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Fork/clone a public book to your library.
    Creates a private copy with attribution.
    """
    result = await service.fork_content(
        content_type="book",
        original_content_id=book_id,
        forking_user_id=current_user["user_id"]
    )
    
    return {
        "message": "Book forked successfully",
        "forked_book": result
    }


@router.post("/decks/{deck_id}/fork")
async def fork_deck(
    deck_id: str,
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Fork/clone a public deck to your library.
    Creates a private copy with attribution.
    """
    result = await service.fork_content(
        content_type="deck",
        original_content_id=deck_id,
        forking_user_id=current_user["user_id"]
    )
    
    return {
        "message": "Deck forked successfully",
        "forked_deck": result
    }


# ========== User Library (Auth Required) ==========

@router.get("/me/liked")
async def get_my_liked_content(
    content_type: Optional[Literal["book", "deck"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    service: PublicContentService = Depends(get_public_service)
):
    """
    Get content I've liked.
    
    Query Parameters:
    - content_type: Filter by "book" or "deck" (optional)
    - page: Page number
    - page_size: Items per page
    """
    result = await service.get_user_liked_content(
        user_id=current_user["user_id"],
        content_type=content_type,
        page=page,
        page_size=page_size
    )
    
    return result
