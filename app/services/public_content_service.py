"""
Service layer for public content operations.
Handles publishing, forking, tracking, and discovery of public Books and Decks.
"""
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from uuid import UUID
from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config.official_publisher import (
    get_official_publisher_name,
    get_official_publisher_user_id,
)
from app.models.PublicContent import (
    ContentFork,
    ContentLike,
    ContentView,
    DeckCuration,
    ForkOutcome,
    PublicCuration,
    PublicMetadata,
    PublicPublisher,
    fork_key,
    fork_status,
    is_official_deck,
    public_curation,
)
from app.models.User import (
    OnboardingActivation,
    coerce_utc,
    normalize_onboarding_state,
    onboarding_activation_update,
)

#: Hard ceiling on any browse page, enforced in the service so no caller can
#: request an unbounded read even if it bypasses the router's Query bound.
MAX_BROWSE_PAGE_SIZE: int = 100

#: Sort value that selects deterministic editorial ordering (ADR-004).
CURATED_SORT: str = "curated"

#: Upper bound on cards copied by one deck fork. Pre-existing limit, named.
MAX_FORK_CARDS: int = 500

#: How long a `pending` fork claim is respected before another request may take
#: it over. A process that dies mid-copy would otherwise hold the durable key
#: forever and permanently block the user from forking that deck. Long enough
#: that a genuinely running copy is never stolen (a 500-card copy is a handful
#: of round trips), short enough that a real crash self-heals on the next retry.
FORK_PENDING_TAKEOVER_SECONDS: int = 120

#: Attempts of the claim loop. Two is exactly enough: one to observe the state,
#: one to act on whatever a concurrent winner wrote in between.
MAX_FORK_CLAIM_ATTEMPTS: int = 2


def _fork_conflict(code: str, **extra: Any) -> HTTPException:
    """Build a 409 with the stable machine-readable code the client switches on."""
    return HTTPException(status_code=409, detail={"code": code, **extra})


def _activation_failed() -> HTTPException:
    """Build the recoverable activation error from the fork contract.

    The deck and its cards are already persisted when this is raised, so the
    client's correct response is to repeat the identical request: the replay
    returns the same deck and finishes the activation (ADR-006).
    """
    return HTTPException(
        status_code=500,
        detail={
            "code": "activation_failed",
            "message": "Deck was forked but onboarding activation did not persist; retry",
        },
    )


def require_official_source(original: Dict[str, Any]) -> None:
    """Reject an onboarding fork whose source is not approved official content.

    Evaluated before anything is claimed or copied, so a source that cannot
    activate never leaves a deck behind either. The predicate itself is
    ONB-002's — approval and official ownership are checked there and nowhere
    else, so this workflow cannot drift from curated browse's definition.
    """
    if not is_official_deck(original, get_official_publisher_user_id()):
        raise _fork_conflict("source_not_official")


def validated_idempotency_key(raw: Optional[str]) -> Optional[str]:
    """Normalise a client `Idempotency-Key` header.

    The header only correlates retries — the durable `(type, source, user)` key
    is what prevents duplicates — so it is optional and every existing caller
    that omits it is unaffected. When one *is* supplied it must be the UUID the
    contract specifies, because a silently accepted malformed value would make
    the diagnostic trail useless.
    """
    if raw is None:
        return None

    candidate = raw.strip()
    try:
        return str(UUID(candidate))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "malformed_idempotency_key",
                "message": "Idempotency-Key must be a UUID",
            },
        )


def _pending_claim_expired(record: Dict[str, Any], now: datetime) -> bool:
    """True when a `pending` claim is old enough to be taken over.

    A record with an unreadable timestamp is treated as expired: an unusable
    claim must not be able to block the key indefinitely.
    """
    claimed_at = coerce_utc(record.get("updated_at")) or coerce_utc(record.get("forked_at"))
    if claimed_at is None:
        return True
    return (now - claimed_at).total_seconds() >= FORK_PENDING_TAKEOVER_SECONDS


def _empty_page(page: int, page_size: int) -> Dict[str, Any]:
    """The ordinary page envelope with no results.

    Uncovered topics are an expected successful outcome (FR-059), so this is a
    normal 200 body rather than an error.
    """
    return {
        "items": [],
        "total": 0,
        "page": page,
        "page_size": page_size,
        "total_pages": 0,
    }


def _strip_stored_curation(doc: dict) -> dict:
    """Remove the raw curation subdocument from a public response.

    Stored curation carries the reviewer's identity and review time. Public
    payloads echo `public_metadata` verbatim, so the raw key is dropped and the
    typed `PublicCuration` projection stays the only curation a client sees.
    """
    metadata = doc.get("public_metadata")
    if isinstance(metadata, dict) and "curation" in metadata:
        doc["public_metadata"] = {
            key: value for key, value in metadata.items() if key != "curation"
        }
    return doc


def _browse_sort_fields(sort_by: str) -> List[tuple]:
    """Map a sort value to its MongoDB sort specification.

    `curated` orders by ascending editorial rank then ascending `_id`, giving a
    stable total order. Popularity is never consulted for curated ordering: at
    launch every seed deck has identical (zero) engagement.
    """
    if sort_by == CURATED_SORT:
        return [("public_metadata.curation.rank", 1), ("_id", 1)]
    if sort_by == "popular":
        return [("public_metadata.views", -1)]
    if sort_by == "top_rated":
        return [
            ("public_metadata.average_rating", -1),
            ("public_metadata.rating_count", -1),
        ]
    return [("published_at", -1)]


def _apply_official_filter(
    query: Dict[str, Any],
    official_publisher_user_id: str,
    category: Optional[str],
) -> None:
    """Restrict a browse query to approved decks from the official account.

    Mirrors the official predicate in `app.models.PublicContent.is_official_deck`;
    `is_public` and `deleted_at` are already constrained by the base query. When
    a category is supplied both it and the curation topic are pinned to it,
    which is index-friendly and equivalent to comparing the two fields. Without
    a category the two fields are compared directly so an approval filed under
    the wrong topic still cannot surface.
    """
    query["user_id"] = official_publisher_user_id
    query["public_metadata.curation.status"] = "approved"

    if category:
        query["public_metadata.curation.topic"] = category
    else:
        query["$expr"] = {
            "$eq": [
                "$public_metadata.curation.topic",
                "$public_metadata.category",
            ]
        }


class PublicContentService:
    """Service for managing public content"""

    def __init__(self, db):
        self.db = db

    def _serialize_doc(self, doc: Optional[dict]) -> Optional[dict]:
        """Convert ObjectId fields to strings for JSON serialization"""
        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        if doc.get("user_id") and isinstance(doc["user_id"], ObjectId):
            doc["user_id"] = str(doc["user_id"])

        # Serialize any list fields that may contain ObjectId references (e.g. Deck.cards)
        for key, value in doc.items():
            if isinstance(value, list):
                doc[key] = [str(item) if isinstance(item, ObjectId) else item for item in value]

        # Handle author/author_name consistency
        if "author" in doc and "author_name" not in doc:
            doc["author_name"] = doc["author"]

        return doc
    
    # ========== Publishing ==========
    
    async def publish_content(
        self,
        content_type: str,  # "book" or "deck"
        content_id: str,
        user_id: str,
        public_metadata: Dict[str, Any]
    ) -> dict:
        """
        Make content public.
        
        Args:
            content_type: "book" or "deck"
            content_id: ID of the content
            user_id: Owner's user ID
            public_metadata: PublicMetadata fields
            
        Returns:
            Updated content document
        """
        collection = self.db[f"{content_type}s"]  # "books" or "decks"
        
        # Verify ownership
        content = await collection.find_one({
            "_id": ObjectId(content_id),
            "user_id": user_id,
            "deleted_at": None
        })
        
        if not content:
            raise HTTPException(status_code=404, detail=f"{content_type.capitalize()} not found")
        
        if content.get("is_public"):
            raise HTTPException(status_code=400, detail="Content is already public")
        
        # Create public metadata.
        # `curation` is trusted editorial state (ADR-004) and is stripped here
        # before validation: publishing must never be able to grant approval,
        # a reviewer, a review time, or an editorial rank, whoever the caller is.
        client_metadata = {
            key: value for key, value in public_metadata.items() if key != "curation"
        }
        metadata = PublicMetadata(**client_metadata)

        # Build public_metadata dict; inject fork attribution if the document
        # carries a forked_from field — this ensures attribution is always
        # present in the published record even if the user never set it manually.
        metadata_dict: dict = metadata.model_dump()
        if content.get("forked_from"):
            metadata_dict["forked_from"] = content["forked_from"]

        # Carry forward any existing editorial curation. Unpublishing and
        # republishing an official deck is an availability action, not an
        # editorial one, so it must not silently erase a completed review.
        existing_metadata = content.get("public_metadata")
        if isinstance(existing_metadata, dict) and existing_metadata.get("curation"):
            metadata_dict["curation"] = existing_metadata["curation"]

        # Update content
        await collection.update_one(
            {"_id": ObjectId(content_id)},
            {
                "$set": {
                    "is_public": True,
                    "published_at": datetime.now(timezone.utc),
                    "public_metadata": metadata_dict,
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )

        # Return updated content
        updated_content = await collection.find_one({"_id": ObjectId(content_id)})
        return self._serialize_doc(updated_content)
    
    async def unpublish_content(
        self,
        content_type: str,
        content_id: str,
        user_id: str
    ) -> dict:
        """Make content private again"""
        collection = self.db[f"{content_type}s"]
        
        # Verify ownership
        content = await collection.find_one({
            "_id": ObjectId(content_id),
            "user_id": user_id,
            "deleted_at": None
        })
        
        if not content:
            raise HTTPException(status_code=404, detail=f"{content_type.capitalize()} not found")
        
        if not content.get("is_public"):
            raise HTTPException(status_code=400, detail="Content is already private")
        
        # Update content
        await collection.update_one(
            {"_id": ObjectId(content_id)},
            {
                "$set": {
                    "is_public": False,
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )
        
        updated_content = await collection.find_one({"_id": ObjectId(content_id)})
        return self._serialize_doc(updated_content)
    
    # ========== Discovery & Browse ==========
    
    def _build_browse_query(
        self,
        content_type: str,
        category: Optional[str],
        tags: Optional[List[str]],
        language: Optional[str],
        difficulty: Optional[str],
        search_query: Optional[str],
        viewer_role: Optional[str],
        viewer_is_beta: bool,
    ) -> Dict[str, Any]:
        """Build the ordinary (non-official) browse filter. Behavior unchanged."""
        query: Dict[str, Any] = {"is_public": True, "deleted_at": None}

        # Access control: content can be restricted to "dev", "beta" or "premium".
        # Unrestricted content is always visible; restricted content only to a
        # viewer holding the matching role.
        restriction_filter: List[dict] = [
            {"public_metadata.restricted_to": None},
            {"public_metadata.restricted_to": {"$exists": False}},
        ]
        if viewer_role == "dev":
            restriction_filter.append({"public_metadata.restricted_to": "dev"})
        if viewer_is_beta:
            restriction_filter.append({"public_metadata.restricted_to": "beta"})
        query["$or"] = restriction_filter

        if category:
            query["public_metadata.category"] = category
        if tags:
            query["public_metadata.tags"] = {"$in": tags}
        if language:
            query["public_metadata.language"] = language
        if difficulty:
            query["public_metadata.difficulty_level"] = difficulty

        if search_query:
            import re
            safe_query = re.escape(search_query)
            # Text search on title and description
            search_filter: List[dict] = [
                {"title" if content_type == "book" else "name": {"$regex": safe_query, "$options": "i"}},
                {"summary" if content_type == "book" else "description": {"$regex": safe_query, "$options": "i"}},
                {"public_metadata.tags": {"$regex": safe_query, "$options": "i"}}
            ]
            # The access restriction and the search are independent predicates
            # that must BOTH hold, and they cannot share the single top-level
            # `$or` key. Assigning the search clause to `query["$or"]` would
            # silently drop the restriction and surface restricted content to
            # viewers who are not entitled to it — visible only when a search
            # term is supplied. `$and` conjoins the two so the access filter
            # survives regardless of which other filters are present.
            query.pop("$or", None)
            query["$and"] = [
                {"$or": restriction_filter},
                {"$or": search_filter},
            ]

        return query

    def _project_deck_public_fields(self, deck: dict) -> dict:
        """Attach the server-derived official projection to a browse item.

        `is_official` is computed here from stored approval plus configured
        publisher identity — it is never read from the document, so a copied or
        hand-written metadata blob cannot claim it. Curation is exposed only for
        decks that actually pass the predicate, keeping editorial state for
        unapproved candidates private.
        """
        official = is_official_deck(deck, get_official_publisher_user_id())

        curation: Optional[PublicCuration] = public_curation(deck) if official else None
        publisher: Optional[PublicPublisher] = None
        if official:
            publisher = PublicPublisher(name=get_official_publisher_name())
        else:
            author = deck.get("author_name") or deck.get("author")
            if isinstance(author, str) and author.strip():
                publisher = PublicPublisher(name=author)

        _strip_stored_curation(deck)
        deck["is_official"] = official
        deck["curation"] = curation.model_dump() if curation else None
        deck["publisher"] = publisher.model_dump() if publisher else None
        return deck

    async def browse_public_content(
        self,
        content_type: str,  # "book" or "deck"
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        language: Optional[str] = None,
        difficulty: Optional[str] = None,
        search_query: Optional[str] = None,
        sort_by: str = "recent",  # "recent", "popular", "top_rated", "curated"
        page: int = 1,
        page_size: int = 20,
        viewer_role: Optional[str] = None,  # User's role for access control
        viewer_is_beta: bool = False,  # User's beta status
        official: bool = False,  # Restrict to approved official curated content
    ) -> Dict[str, Any]:
        """
        Browse and search public content.

        With `official=True` the result is restricted to editorially approved
        decks owned by the configured Nowry publisher (ADR-004). Every other
        code path behaves exactly as before.

        Returns:
            {
                "items": [...],
                "total": 100,
                "page": 1,
                "page_size": 20,
                "total_pages": 5
            }
        """
        collection = self.db[f"{content_type}s"]
        page_size = max(1, min(page_size, MAX_BROWSE_PAGE_SIZE))

        query = self._build_browse_query(
            content_type, category, tags, language, difficulty,
            search_query, viewer_role, viewer_is_beta,
        )

        if official:
            official_publisher_user_id = get_official_publisher_user_id()
            if not official_publisher_user_id:
                # Nothing can be official without a configured publisher.
                return _empty_page(page, page_size)
            _apply_official_filter(query, official_publisher_user_id, category)

        # Count total
        total = await collection.count_documents(query)

        # Paginate
        skip = (page - 1) * page_size
        items = await collection.find(query).sort(
            _browse_sort_fields(sort_by)
        ).skip(skip).limit(page_size).to_list(page_size)

        # Convert ObjectIds to strings for JSON serialization
        items = [self._serialize_doc(item) for item in items]
        if content_type == "deck":
            items = [self._project_deck_public_fields(item) for item in items]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }

    # ========== Editorial Curation (trusted path) ==========

    async def set_deck_curation(
        self,
        deck_id: str,
        curation: Dict[str, Any],
        reviewer_id: str,
    ) -> dict:
        """Write editorial curation metadata for an official-account deck.

        This is the only path that can write `public_metadata.curation`. It is
        reachable only through the admin-gated route; the ordinary publish
        endpoint has no curation field and strips the key defensively. The
        reviewer identity and review time come from the authenticated caller and
        the server clock, never from the request body.
        """
        official_publisher_user_id = get_official_publisher_user_id()
        if not official_publisher_user_id:
            raise HTTPException(
                status_code=503,
                detail={"code": "official_publisher_not_configured"},
            )

        deck = await self._load_curatable_deck(deck_id, official_publisher_user_id)

        # Reviewer and review time are server-owned: drop any supplied value
        # before validation so no caller of this service can forge provenance.
        editorial = {
            key: value
            for key, value in curation.items()
            if key not in ("reviewed_by", "reviewed_at")
        }
        record = DeckCuration(
            **editorial,
            reviewed_by=reviewer_id,
            reviewed_at=datetime.now(timezone.utc),
        )
        if record.status == "approved":
            self._assert_approvable(deck, record)

        await self._write_deck_curation(deck["_id"], record)

        updated = await self.db["decks"].find_one({"_id": deck["_id"]})
        return {
            "deck_id": str(deck["_id"]),
            **record.model_dump(),
            "is_official": is_official_deck(updated or {}, official_publisher_user_id),
        }

    async def _write_deck_curation(self, deck_oid: ObjectId, record: DeckCuration) -> None:
        """Persist a validated curation record.

        The unique partial index on approved `(topic, rank)` is what actually
        guarantees unambiguous editorial ordering, so its rejection is surfaced
        as a conflict rather than a server error.
        """
        try:
            await self.db["decks"].update_one(
                {"_id": deck_oid},
                {
                    "$set": {
                        "public_metadata.curation": record.model_dump(),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
        except DuplicateKeyError:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "curated_rank_taken",
                    "topic": record.topic,
                    "rank": record.rank,
                },
            )

    async def _load_curatable_deck(
        self,
        deck_id: str,
        official_publisher_user_id: str,
    ) -> dict:
        """Fetch a deck that curation may legitimately be written to."""
        try:
            deck_oid = ObjectId(deck_id)
        except Exception:
            raise HTTPException(status_code=404, detail={"code": "deck_not_found"})

        deck = await self.db["decks"].find_one({"_id": deck_oid, "deleted_at": None})
        if not deck:
            raise HTTPException(status_code=404, detail={"code": "deck_not_found"})

        if str(deck.get("user_id")) != official_publisher_user_id:
            raise HTTPException(
                status_code=403,
                detail={"code": "not_official_publisher"},
            )

        # Curation lives inside public_metadata, which only exists once the deck
        # has been published by the official account.
        if deck.get("is_public") is not True or not isinstance(deck.get("public_metadata"), dict):
            raise HTTPException(status_code=400, detail={"code": "deck_not_public"})

        return deck

    def _assert_approvable(self, deck: dict, record: DeckCuration) -> None:
        """Reject approvals that could never satisfy the official predicate.

        The topic must match the category the deck is browsed under, and a deck
        with no cards cannot support a first study session (FR-057). Card
        sufficiency beyond "not empty" stays an editorial judgment, so no
        universal minimum is imposed here.
        """
        metadata = deck.get("public_metadata") or {}
        if record.topic != metadata.get("category"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "curation_topic_category_mismatch",
                    "topic": record.topic,
                    "category": metadata.get("category"),
                },
            )

        if not deck.get("total_cards"):
            raise HTTPException(
                status_code=400,
                detail={"code": "deck_has_no_cards"},
            )


    async def get_public_content_by_id(
        self,
        content_type: str,
        content_id: str,
        viewer_user_id: Optional[str] = None,
        viewer_role: Optional[str] = None,
        viewer_is_beta: bool = False,
        track_view: bool = True
    ) -> dict:
        """
        Get a single public content item.
        Optionally track the view.
        Respects access control restrictions.
        """
        collection = self.db[f"{content_type}s"]
        
        content = await collection.find_one({
            "_id": ObjectId(content_id),
            "is_public": True,
            "deleted_at": None
        })
        
        if not content:
            return None
        
        # Check access control
        restricted_to = content.get("public_metadata", {}).get("restricted_to")
        
        if restricted_to:
            # Content is restricted - check if viewer has access
            has_access = False
            
            if restricted_to == "dev" and viewer_role == "dev":
                has_access = True
            elif restricted_to == "beta" and viewer_is_beta:
                has_access = True
            elif restricted_to == "premium":
                # TODO: Check premium subscription status
                pass
            
            if not has_access:
                raise HTTPException(
                    status_code=403,
                    detail=f"This content is restricted to {restricted_to} users only"
                )
        
        if not content:
            raise HTTPException(status_code=404, detail="Public content not found")
        
        # Track view — deduplicated within a 60-second window to prevent
        # double-counting from React StrictMode double-mounts in development
        # and from accidental rapid refreshes in production.
        if track_view:
            already_tracked = await self._recent_view_exists(
                content_type, content_id, viewer_user_id
            )
            if not already_tracked:
                await self.track_view(content_type, content_id, viewer_user_id)
                await collection.update_one(
                    {"_id": ObjectId(content_id)},
                    {"$inc": {"public_metadata.views": 1}}
                )

        # Populate user_liked for the viewer
        user_liked: bool = False
        if viewer_user_id:
            existing_like = await self.db["content_likes"].find_one({
                "content_type": content_type,
                "content_id": content_id,
                "user_id": viewer_user_id
            })
            user_liked = existing_like is not None

        serialized = _strip_stored_curation(self._serialize_doc(content))
        serialized["user_liked"] = user_liked
        return serialized
    
    # ========== Engagement ==========
    
    async def like_content(
        self,
        content_type: str,
        content_id: str,
        user_id: str
    ) -> dict:
        """Like/favorite public content"""
        # Check if already liked
        existing_like = await self.db["content_likes"].find_one({
            "content_type": content_type,
            "content_id": content_id,
            "user_id": user_id
        })
        
        if existing_like:
            raise HTTPException(status_code=400, detail="Already liked")
        
        # Create like
        like = ContentLike(
            content_type=content_type,
            content_id=content_id,
            user_id=user_id
        )
        
        await self.db["content_likes"].insert_one(like.model_dump(by_alias=True))
        
        # Increment like count
        collection = self.db[f"{content_type}s"]
        await collection.update_one(
            {"_id": ObjectId(content_id)},
            {"$inc": {"public_metadata.likes": 1}}
        )
        
        return {"message": "Content liked successfully"}
    
    async def unlike_content(
        self,
        content_type: str,
        content_id: str,
        user_id: str
    ) -> dict:
        """Remove like from public content"""
        result = await self.db["content_likes"].delete_one({
            "content_type": content_type,
            "content_id": content_id,
            "user_id": user_id
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Like not found")
        
        # Decrement like count
        collection = self.db[f"{content_type}s"]
        await collection.update_one(
            {"_id": ObjectId(content_id)},
            {"$inc": {"public_metadata.likes": -1}}
        )
        
        return {"message": "Like removed successfully"}
    
    async def _recent_view_exists(
        self,
        content_type: str,
        content_id: str,
        viewer_user_id: Optional[str] = None,
        window_seconds: int = 60
    ) -> bool:
        """
        Return True if a view record already exists for this content + viewer
        within the last `window_seconds` seconds.

        For anonymous viewers (no user_id) this always returns False — we cannot
        deduplicate without a stable identifier, so anonymous rapid-refreshes are
        accepted as a known limitation.
        """
        if not viewer_user_id:
            return False

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)

        existing = await self.db["content_views"].find_one({
            "content_type": content_type,
            "content_id": content_id,
            "viewer_user_id": viewer_user_id,
            "viewed_at": {"$gte": cutoff}
        })
        return existing is not None

    async def track_view(
        self,
        content_type: str,
        content_id: str,
        viewer_user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ):
        """Track a content view (for analytics)"""
        view = ContentView(
            content_type=content_type,
            content_id=content_id,
            viewer_user_id=viewer_user_id,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        await self.db["content_views"].insert_one(view.model_dump(by_alias=True))
    
    # ========== Forking/Cloning ==========

    async def fork_content(
        self,
        content_type: str,
        original_content_id: str,
        forking_user_id: str,
        idempotency_key: Optional[str] = None,
        onboarding_context: bool = False,
    ) -> ForkOutcome:
        """
        Fork public content into the caller's library, idempotently (ADR-005).

        Ordinary forks are unchanged. With `onboarding_context`, this workflow
        additionally owns onboarding activation (ADR-006), in a fixed order:

        1. the source must satisfy ONB-002's official predicate, checked before
           anything is claimed, so a source that cannot activate cannot leave a
           deck behind either → `409 source_not_official`;
        2. the copy must have completed — activation runs only on a resolved
           outcome, never on a `pending` claim or a `fork_in_progress` retry;
        3. activation must persist before this returns, so a successful
           response always means deck, cards and activation are all durable.

        Activation runs on *both* resolutions, first completion and replay,
        which is what makes a retry repair an activation that failed after the
        deck was already created.
        """
        collection = self.db[f"{content_type}s"]
        original = await self._load_forkable_original(
            collection, original_content_id, forking_user_id
        )
        if onboarding_context:
            require_official_source(original)

        outcome = await self._resolve_fork(
            original,
            original_content_id,
            content_type,
            forking_user_id,
            idempotency_key,
            collection,
        )

        if onboarding_context:
            outcome.onboarding = await self._activate_onboarding(forking_user_id)
        return outcome

    async def _resolve_fork(
        self,
        original: dict,
        original_content_id: str,
        content_type: str,
        forking_user_id: str,
        idempotency_key: Optional[str],
        collection,
    ) -> ForkOutcome:
        """Claim, copy or replay the fork itself (ADR-005).

        The durable identity of a fork is `(content type, source, user)`. A
        `pending` record on that key is claimed *before* any content is copied,
        so concurrent requests cannot both materialise a private copy: the
        unique index rejects the second claim and that request resolves the
        winner's record instead of creating a deck of its own.

        Outcomes:

        - no record            → claim, copy, complete → `created=True`
        - completed, live copy → replay the same deck  → `created=False`
                                 (books keep the historical `409 already_forked`)
        - completed, copy gone → stale-record recreation → `created=True`
        - failed / abandoned   → discard the partial copy, recreate
        - pending and fresh    → `409 fork_in_progress`, recoverable by retry
        """
        key = fork_key(content_type, original_content_id, forking_user_id)

        for _ in range(MAX_FORK_CLAIM_ATTEMPTS):
            existing = await self.db["content_forks"].find_one(key)

            if existing is None:
                claimed = await self._insert_fork_claim(key, original, idempotency_key)
                if claimed is None:
                    continue  # another request won the claim; resolve its record
                return await self._copy_into_claim(claimed, original, content_type, collection)

            replay = await self._replay_completed_fork(existing, content_type, collection)
            if replay is not None:
                return replay

            reclaimed = await self._reclaim_stale_fork(
                existing, content_type, collection, idempotency_key
            )
            if reclaimed is None:
                raise _fork_conflict("fork_in_progress")
            return await self._copy_into_claim(reclaimed, original, content_type, collection)

        raise _fork_conflict("fork_in_progress")

    async def _load_forkable_original(
        self,
        collection,
        original_content_id: str,
        forking_user_id: str,
    ) -> dict:
        """Load the public source document, rejecting unknown content and self-forks."""
        try:
            original_oid = ObjectId(str(original_content_id))
        except Exception:
            raise HTTPException(status_code=404, detail="Public content not found")

        original = await collection.find_one({
            "_id": original_oid,
            "is_public": True,
            "deleted_at": None,
        })
        if not original:
            raise HTTPException(status_code=404, detail="Public content not found")

        if str(original.get("user_id")) == str(forking_user_id):
            raise HTTPException(status_code=400, detail="cannot_fork_own_content")

        return original

    async def _live_fork_target(self, record: Dict[str, Any], collection) -> Optional[dict]:
        """Return the fork's content document while it still exists and is not deleted."""
        forked_id = record.get("forked_content_id")
        if not forked_id:
            return None
        try:
            forked_oid = ObjectId(str(forked_id))
        except Exception:
            return None
        return await collection.find_one({"_id": forked_oid, "deleted_at": None})

    async def _replay_completed_fork(
        self,
        record: Dict[str, Any],
        content_type: str,
        collection,
    ) -> Optional[ForkOutcome]:
        """Answer a repeat request for a fork this user already completed.

        Returns `None` when the record is not a completed fork of live content,
        which means the caller must reclaim and recreate it.

        Deck forks replay as a success carrying the same deck: a retrying client
        cannot distinguish a lost response from an unwanted duplicate, and
        ADR-005 resolves that in favour of idempotent success. Book forks keep
        the historical `409 already_forked` because ADR-005 scopes the response
        change to decks and existing book clients must stay compatible.
        """
        if fork_status(record) != "completed":
            return None

        live = await self._live_fork_target(record, collection)
        if live is None:
            return None

        if content_type != "deck":
            raise _fork_conflict(
                "already_forked",
                forked_content_id=str(record.get("forked_content_id")),
            )

        return ForkOutcome(created=False, content=self._serialize_doc(live))

    async def _insert_fork_claim(
        self,
        key: Dict[str, str],
        original: dict,
        idempotency_key: Optional[str],
    ) -> Optional[dict]:
        """Insert a `pending` claim on the durable key.

        Returns `None` when the unique index rejects the insert, i.e. a
        concurrent request claimed the same key first. That rejection — not the
        preceding read — is the guarantee, because only the database observes
        both requests.
        """
        record = ContentFork(
            original_content_type=key["original_content_type"],
            original_content_id=key["original_content_id"],
            original_creator_id=str(original.get("user_id")),
            forked_by_user_id=key["forked_by_user_id"],
            forked_content_id=None,
            status="pending",
            idempotency_key=idempotency_key,
        )
        document = record.model_dump(by_alias=True)

        try:
            await self.db["content_forks"].insert_one(document)
        except DuplicateKeyError:
            return None
        return document

    async def _reclaim_stale_fork(
        self,
        record: Dict[str, Any],
        content_type: str,
        collection,
        idempotency_key: Optional[str],
    ) -> Optional[dict]:
        """Take over a record whose fork is not live, and clear its leftovers.

        Reclaimable states: `failed`, `completed` whose content is gone (the
        existing stale-record rule), and `pending` abandoned past the takeover
        window. A fresh `pending` claim belongs to a request that is still
        running and is never stolen.

        The update is a compare-and-set on the exact values just observed, so
        two requests racing to reclaim the same stale record cannot both win —
        the loser sees `None` and is told to retry.
        """
        status = fork_status(record)
        now = datetime.now(timezone.utc)
        if status == "pending" and not _pending_claim_expired(record, now):
            return None

        claimed = await self.db["content_forks"].find_one_and_update(
            {
                "_id": record["_id"],
                "status": record.get("status"),
                "forked_content_id": record.get("forked_content_id"),
                "updated_at": record.get("updated_at"),
            },
            {"$set": {
                "status": "pending",
                "forked_content_id": None,
                "idempotency_key": idempotency_key,
                "failure_code": None,
                "forked_at": now,
                "updated_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            return None

        await self._discard_partial_fork(record, content_type, collection)
        return claimed

    async def _discard_partial_fork(
        self,
        record: Dict[str, Any],
        content_type: str,
        collection,
    ) -> None:
        """Delete the live leftovers of a fork attempt this request just took over.

        Only content that is still live, owned by the same user and attributed
        to the same source is removed — that is the partial copy this workflow
        created and abandoned, never a document the user chose to keep. Content
        the user deleted themselves is already soft-deleted and is left to its
        retention window. Without this, a retry after a partial copy would leave
        two visible decks for one fork.
        """
        forked_id = record.get("forked_content_id")
        if not forked_id:
            return
        try:
            forked_oid = ObjectId(str(forked_id))
        except Exception:
            return

        owner = record.get("forked_by_user_id")
        partial = await collection.find_one({
            "_id": forked_oid,
            "user_id": owner,
            "deleted_at": None,
        })
        if partial is None:
            return
        attribution = (partial.get("forked_from") or {}).get("id")
        if str(attribution) != str(record.get("original_content_id")):
            return

        if content_type == "deck":
            await self.db["cards"].delete_many({
                "deck_id": {"$in": [str(forked_oid), forked_oid]},
                "user_id": owner,
            })
        await collection.delete_one({"_id": forked_oid, "user_id": owner})

    async def _copy_into_claim(
        self,
        record: Dict[str, Any],
        original: dict,
        content_type: str,
        collection,
    ) -> ForkOutcome:
        """Materialise the content for a claim this request holds.

        The claim stays `pending` until every document is written, so an
        interrupted copy is never reported as success and never replayed as a
        completed fork — the next attempt sees a partial record and recreates it.
        """
        try:
            forked_oid = await self._insert_fork_copy(original, content_type, collection, record)
            if content_type == "deck":
                await self._copy_fork_cards(original, forked_oid, record, collection)
            await self._complete_fork_claim(record, forked_oid, original, collection)
        except HTTPException:
            raise
        except Exception as error:
            await self._fail_fork_claim(record, type(error).__name__)
            raise HTTPException(
                status_code=500,
                detail={"code": "fork_failed", "message": "Fork could not be completed"},
            )

        forked = await collection.find_one({"_id": forked_oid})
        return ForkOutcome(created=True, content=self._serialize_doc(forked))

    async def _insert_fork_copy(
        self,
        original: dict,
        content_type: str,
        collection,
        record: Dict[str, Any],
    ) -> ObjectId:
        """Insert the private copy and record its id on the pending claim."""
        forking_user_id = record["forked_by_user_id"]
        now = datetime.now(timezone.utc)

        forked_content = dict(original)
        forked_content.pop("_id")
        forked_content["user_id"] = forking_user_id
        forked_content["is_public"] = False
        forked_content["published_at"] = None
        forked_content["public_metadata"] = None
        forked_content["created_at"] = now
        forked_content["updated_at"] = now

        title_field = "title" if content_type == "book" else "name"
        forked_content[title_field] = f"{forked_content[title_field]} (Forked)"

        original_user_id = original.get("user_id")
        forked_content["forked_from"] = {
            "id": str(original["_id"]),
            "title": original.get("title") or original.get("name"),
            "author_name": await self._original_author_name(original_user_id),
            "author_id": str(original_user_id) if original_user_id else None,
        }

        # For decks, reset card refs — they are repopulated after the card copy
        if content_type == "deck":
            forked_content["cards"] = []
            forked_content["total_cards"] = 0

        result = await collection.insert_one(forked_content)

        # Publish the id on the still-pending claim so an interrupted attempt
        # leaves a trail its successor can clean up.
        await self.db["content_forks"].update_one(
            {"_id": record["_id"]},
            {"$set": {
                "forked_content_id": str(result.inserted_id),
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return result.inserted_id

    async def _original_author_name(self, original_user_id: Any) -> Optional[str]:
        """Resolve the source author's display name for fork attribution."""
        if not original_user_id:
            return None
        try:
            user_oid = (
                original_user_id
                if isinstance(original_user_id, ObjectId)
                else ObjectId(str(original_user_id))
            )
            user_doc = await self.db["users"].find_one({"_id": user_oid})
        except Exception:
            user_doc = None
        if not user_doc:
            return None
        return (
            user_doc.get("full_name")
            or user_doc.get("display_name")
            or user_doc.get("username")
            or user_doc.get("displayName")
            or "Anonymous User"
        )

    async def _copy_fork_cards(
        self,
        original: dict,
        forked_oid: ObjectId,
        record: Dict[str, Any],
        collection,
    ) -> None:
        """Copy the source deck's cards to the fork with SRS state reset."""
        original_oid = original["_id"]
        forking_user_id = record["forked_by_user_id"]
        original_cards = await self.db["cards"].find(
            {"deck_id": {"$in": [original_oid, str(original_oid)]}}
        ).to_list(length=MAX_FORK_CARDS)

        if not original_cards:
            return

        now = datetime.now(timezone.utc)
        new_cards: List[dict] = []
        for card in original_cards:
            new_card = dict(card)
            new_card.pop("_id")
            new_card["deck_id"] = str(forked_oid)
            new_card["user_id"] = forking_user_id
            new_card["created_at"] = now
            new_card["updated_at"] = now
            # Reset SRS state for the new owner
            new_card["next_review"] = None
            new_card["last_reviewed"] = None
            new_card["introduced_at"] = None
            new_card["interval"] = 1
            new_card["ease_factor"] = 2.5
            new_card["repetitions"] = 0
            new_cards.append(new_card)

        insert_result = await self.db["cards"].insert_many(new_cards)

        await collection.update_one(
            {"_id": forked_oid},
            {"$set": {
                "total_cards": len(new_cards),
                "cards": [str(oid) for oid in insert_result.inserted_ids],
            }},
        )

    async def _complete_fork_claim(
        self,
        record: Dict[str, Any],
        forked_oid: ObjectId,
        original: dict,
        collection,
    ) -> None:
        """Mark the claim completed and count the fork.

        Completion is the last write of the workflow: only after it can a later
        request replay this fork instead of recreating it. The popularity
        counter is incremented here so a replay never inflates it.
        """
        await self.db["content_forks"].update_one(
            {"_id": record["_id"]},
            {"$set": {
                "status": "completed",
                "forked_content_id": str(forked_oid),
                "failure_code": None,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        await collection.update_one(
            {"_id": original["_id"]},
            {"$inc": {"public_metadata.forks": 1}},
        )

    async def _fail_fork_claim(self, record: Dict[str, Any], failure_code: str) -> None:
        """Record why a claim could not be completed, leaving it recreatable.

        The record keeps whatever `forked_content_id` the attempt reached so the
        next attempt can discard that partial copy.
        """
        await self.db["content_forks"].update_one(
            {"_id": record["_id"]},
            {"$set": {
                "status": "failed",
                "failure_code": failure_code,
                "updated_at": datetime.now(timezone.utc),
            }},
        )

    # ========== Onboarding activation (ADR-006) ==========

    async def _activate_onboarding(self, user_id: str) -> OnboardingActivation:
        """Persist onboarding activation for a verified, completed fork.

        Called only after the copy is durable, so activation can never be a
        consequence of merely reaching a screen (FR-006). Idempotent: a replay
        that finds the user already activated returns the *original*
        `activated_at` rather than moving it.

        A failure here leaves the deck in place and raises the recoverable
        `500 activation_failed`; the identical retry replays the same fork and
        completes the activation.
        """
        now = datetime.now(timezone.utc)
        try:
            user = await self._persist_onboarding_activation(user_id, now)
        except Exception:
            raise _activation_failed()

        if user is None:
            raise _activation_failed()

        state = normalize_onboarding_state(user, now)
        if state.status != "activated" or state.activated_at is None:
            # The write reported success but the stored document does not carry
            # the transition; reporting success anyway would strand the journey.
            raise _activation_failed()

        return OnboardingActivation(activated_at=state.activated_at)

    async def _persist_onboarding_activation(
        self,
        user_id: str,
        now: datetime,
    ) -> Optional[dict]:
        """Apply the activation `$set`, or re-read an already-activated user.

        The filter is the idempotency guarantee: a user who already carries an
        `activated_at` is not matched, so concurrent retries cannot overwrite
        the first activation time. A legacy `wizard_completed=True` user has no
        `activated_at`, which Mongo matches as null, so the same call also
        repairs that document to the full typed shape.

        Every activation field lands in one update on one document, so no
        reader can observe a half-activated user.
        """
        object_id = ObjectId(str(user_id))
        users = self.db["users"]

        updated = await users.find_one_and_update(
            {"_id": object_id, "onboarding.activated_at": None},
            {"$set": onboarding_activation_update(now)},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return updated

        return await users.find_one({"_id": object_id})

    # ========== User Libraries ==========
    
    async def get_user_liked_content(
        self,
        user_id: str,
        content_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> Dict[str, Any]:
        """Get content liked by a user"""
        query = {"user_id": user_id}
        
        if content_type:
            query["content_type"] = content_type
        
        total = await self.db["content_likes"].count_documents(query)
        skip = (page - 1) * page_size
        
        likes = await self.db["content_likes"].find(query).sort("created_at", -1).skip(skip).limit(page_size).to_list(page_size)
        
        # Fetch actual content
        items = []
        for like in likes:
            collection = self.db[f"{like['content_type']}s"]
            content = await collection.find_one({
                "_id": ObjectId(like["content_id"]),
                "is_public": True,
                "deleted_at": None
            })
            if content:
                items.append(self._serialize_doc(content))
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
