"""
Service layer for public content operations.
Handles publishing, forking, tracking, and discovery of public Books and Decks.
"""
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from bson import ObjectId
from fastapi import HTTPException

from app.models.PublicContent import PublicMetadata, ContentFork, ContentLike, ContentView


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
        
        # Create public metadata
        metadata = PublicMetadata(**public_metadata)
        
        # Build public_metadata dict; inject fork attribution if the document
        # carries a forked_from field — this ensures attribution is always
        # present in the published record even if the user never set it manually.
        metadata_dict: dict = metadata.dict()
        if content.get("forked_from"):
            metadata_dict["forked_from"] = content["forked_from"]

        # Update content
        await collection.update_one(
            {"_id": ObjectId(content_id)},
            {
                "$set": {
                    "is_public": True,
                    "published_at": datetime.utcnow(),
                    "public_metadata": metadata_dict,
                    "updated_at": datetime.utcnow()
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
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        updated_content = await collection.find_one({"_id": ObjectId(content_id)})
        return self._serialize_doc(updated_content)
    
    # ========== Discovery & Browse ==========
    
    async def browse_public_content(
        self,
        content_type: str,  # "book" or "deck"
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        language: Optional[str] = None,
        difficulty: Optional[str] = None,
        search_query: Optional[str] = None,
        sort_by: str = "recent",  # "recent", "popular", "top_rated"
        page: int = 1,
        page_size: int = 20,
        viewer_role: Optional[str] = None,  # User's role for access control
        viewer_is_beta: bool = False  # User's beta status
    ) -> Dict[str, Any]:
        """
        Browse and search public content.
        
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
        
        # Build query
        query = {
            "is_public": True,
            "deleted_at": None
        }
        
        # Access control: Filter by restricted_to field
        # Content can be restricted to: "dev", "beta", or "premium" users
        restriction_filter = []
        
        # 1. Always include unrestricted content (restricted_to is None or doesn't exist)
        restriction_filter.append({"public_metadata.restricted_to": None})
        restriction_filter.append({"public_metadata.restricted_to": {"$exists": False}})
        
        # 2. If user is dev, they can see dev-restricted content
        if viewer_role == "dev":
            restriction_filter.append({"public_metadata.restricted_to": "dev"})
        
        # 3. If user is beta, they can see beta-restricted content
        if viewer_is_beta:
            restriction_filter.append({"public_metadata.restricted_to": "beta"})
        
        # Apply restriction filter
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
            query["$or"] = [
                {"title" if content_type == "book" else "name": {"$regex": safe_query, "$options": "i"}},
                {"summary" if content_type == "book" else "description": {"$regex": safe_query, "$options": "i"}},
                {"public_metadata.tags": {"$regex": safe_query, "$options": "i"}}
            ]
        
        # Determine sort
        sort_field = []
        if sort_by == "recent":
            sort_field = [("published_at", -1)]
        elif sort_by == "popular":
            sort_field = [("public_metadata.views", -1)]
        elif sort_by == "top_rated":
            sort_field = [("public_metadata.average_rating", -1), ("public_metadata.rating_count", -1)]
        else:
            sort_field = [("published_at", -1)]
        
        # Count total
        total = await collection.count_documents(query)
        
        # Paginate
        skip = (page - 1) * page_size
        items = await collection.find(query).sort(sort_field).skip(skip).limit(page_size).to_list(page_size)
        
        # Convert ObjectIds to strings for JSON serialization
        items = [self._serialize_doc(item) for item in items]
        
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    
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

        serialized = self._serialize_doc(content)
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
        
        await self.db["content_likes"].insert_one(like.dict(by_alias=True))
        
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

        cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)

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
        
        await self.db["content_views"].insert_one(view.dict(by_alias=True))
    
    # ========== Forking/Cloning ==========
    
    async def fork_content(
        self,
        content_type: str,
        original_content_id: str,
        forking_user_id: str
    ) -> dict:
        """
        Fork/clone public content to user's library.
        Creates a copy with attribution to original.
        """
        collection = self.db[f"{content_type}s"]
        
        # Get original content
        original = await collection.find_one({
            "_id": ObjectId(original_content_id),
            "is_public": True,
            "deleted_at": None
        })
        
        if not original:
            raise HTTPException(status_code=404, detail="Public content not found")

        # FIX 1 — Prevent self-fork
        if str(original.get("user_id")) == str(forking_user_id):
            raise HTTPException(
                status_code=400,
                detail="cannot_fork_own_content"
            )

        # FIX 2 — Prevent duplicate fork
        existing_fork = await self.db["content_forks"].find_one({
            "original_content_id": str(original_content_id),
            "forked_by_user_id": str(forking_user_id)
        })

        if existing_fork:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_forked",
                    "forked_content_id": str(existing_fork["forked_content_id"])
                }
            )

        # Create a copy
        forked_content = dict(original)
        forked_content.pop("_id")  # Remove original ID
        forked_content["user_id"] = forking_user_id  # New owner
        forked_content["is_public"] = False  # Forked content is private by default
        forked_content["published_at"] = None
        forked_content["public_metadata"] = None
        forked_content["created_at"] = datetime.utcnow()
        forked_content["updated_at"] = datetime.utcnow()
        
        # Add attribution
        title_field = "title" if content_type == "book" else "name"
        forked_content[title_field] = f"{forked_content[title_field]} (Forked)"

        # Look up the original author's display name from the users collection
        author_name: Optional[str] = None
        original_user_id = original.get("user_id")
        if original_user_id:
            try:
                user_oid = (
                    ObjectId(str(original_user_id))
                    if not isinstance(original_user_id, ObjectId)
                    else original_user_id
                )
                user_doc = await self.db["users"].find_one({"_id": user_oid})
            except Exception:
                user_doc = None
            if user_doc:
                author_name = (
                    user_doc.get("full_name")
                    or user_doc.get("display_name")
                    or user_doc.get("username")
                    or user_doc.get("displayName")
                    or "Anonymous User"
                )

        # Record immutable fork attribution before insert
        forked_content["forked_from"] = {
            "id": str(original["_id"]),
            "title": original.get("title") or original.get("name"),
            "author_name": author_name,
            "author_id": str(original_user_id) if original_user_id else None,
        }

        # For decks, reset card refs — they will be repopulated after insert
        if content_type == "deck":
            forked_content["cards"] = []
            forked_content["total_cards"] = 0

        # Insert forked content
        result = await collection.insert_one(forked_content)
        
        # Track the fork
        fork_record = ContentFork(
            original_content_type=content_type,
            original_content_id=original_content_id,
            original_creator_id=str(original["user_id"]),
            forked_content_id=str(result.inserted_id),
            forked_by_user_id=forking_user_id
        )
        
        await self.db["content_forks"].insert_one(fork_record.dict(by_alias=True))
        
        # Increment fork count
        await collection.update_one(
            {"_id": ObjectId(original_content_id)},
            {"$inc": {"public_metadata.forks": 1}}
        )

        # Copy cards for deck forks
        if content_type == "deck":
            forked_deck_id = str(result.inserted_id)
            original_cards = await self.db["cards"].find(
                {"deck_id": {"$in": [ObjectId(original_content_id), original_content_id]}}
            ).to_list(length=500)

            if original_cards:
                now = datetime.utcnow()
                new_cards: List[dict] = []
                for card in original_cards:
                    new_card = dict(card)
                    new_card.pop("_id")
                    new_card["deck_id"] = forked_deck_id
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
                new_card_ids = [str(oid) for oid in insert_result.inserted_ids]

                await collection.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {
                        "total_cards": len(new_cards),
                        "cards": new_card_ids
                    }}
                )

        # Return the forked content
        forked = await collection.find_one({"_id": result.inserted_id})
        return self._serialize_doc(forked)
    
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
