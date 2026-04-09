"""
Public Content API Router
Handles browse, publish, fork, and engagement for public Books and Decks
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field
from bson import ObjectId

from app.config.database import db
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
    sort_by: Literal["recent", "popular", "top_rated"] = "recent"


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


@router.get("/decks")
async def browse_public_decks(
    category: Optional[str] = None,
    tags: Optional[str] = None,
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
    Browse public decks.
    No authentication required, but logged-in users see more content based on their role.
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
        viewer_is_beta=viewer_is_beta
    )
    
    return result


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
        public_metadata=publish_data.dict()
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
        public_metadata=publish_data.dict()
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
