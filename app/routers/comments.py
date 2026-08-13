"""
Comments router — user-private, text-anchored annotations on a resource.

Endpoints:
    POST   /v1/comments                                    — create a comment
    GET    /v1/comments?resource_type=book&resource_id={id} — list the caller's comments on a resource
    PATCH  /v1/comments/{comment_id}                        — partial update (body / resolved)
    DELETE /v1/comments/{comment_id}                        — soft-delete

Security (non-negotiable — see Comment model docstring and PATTERNS):
    GET /public/books/{book_id} (app/routers/public_content.py) serves a shared
    book to other viewers at the SAME book_id as the owner — it's a view-in-place,
    not a copy (forking is what creates a new _id, see services/public_content_service.py).
    That means comments MUST always be filtered by resource_id AND user_id
    together; filtering by resource_id alone would leak one user's private
    notes to everyone else viewing the same shared book. Every query below
    enforces both.
"""
from datetime import datetime, timezone
from typing import Dict, List

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.collection import Collection

from app.auth.dependencies import require_ownership
from app.auth.firebase_auth import get_firebase_user
from app.config.database import books_collection, comments_collection
from app.models.Comment import (
    CommentCreate,
    CommentResponse,
    CommentUpdate,
    ResourceType,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    tags=["comments"],
    dependencies=[Depends(get_firebase_user)],
)


def get_comments_collection() -> Collection:
    return comments_collection


# Extension point for future resource types — every entry MUST be a collection
# whose documents carry "user_id" and "is_public" fields, since
# `_verify_resource_visible` relies on both to authorize comment creation.
_RESOURCE_COLLECTIONS: Dict[str, Collection] = {
    "book": books_collection,
}


def _to_response(doc: dict) -> CommentResponse:
    """Convert a raw MongoDB comment document into the public response shape."""
    return CommentResponse(
        id=str(doc["_id"]),
        user_id=doc["user_id"],
        resource_type=doc["resource_type"],
        resource_id=doc["resource_id"],
        anchor=doc["anchor"],
        body=doc["body"],
        resolved=doc.get("resolved", False),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


async def _verify_resource_visible(resource_type: ResourceType, resource_id: str, user_id: str) -> None:
    """
    Verify the target resource exists and is visible to the caller (own OR public).

    Mirrors `require_public_or_ownership` in app/auth/dependencies.py, but that
    dependency factory reads its id from a path param — here resource_type and
    resource_id arrive in the POST body, so the check is reimplemented inline
    against the same predicate. Raises 404 (not 403) on any failure so callers
    can't probe for the existence of resources they can't see.
    """
    collection = _RESOURCE_COLLECTIONS.get(resource_type)
    if collection is None:
        raise HTTPException(status_code=400, detail=f"Unsupported resource_type: {resource_type}")

    try:
        obj_id = ObjectId(resource_id)
    except Exception:
        obj_id = resource_id

    doc = await collection.find_one({"_id": obj_id, "deleted_at": None})
    if not doc:
        raise HTTPException(status_code=404, detail="Resource not found")

    is_public = doc.get("is_public", False)
    if doc.get("user_id") != user_id and not is_public:
        raise HTTPException(status_code=404, detail="Resource not found")


@router.post("", response_model=CommentResponse, status_code=201)
async def create_comment(
    payload: CommentCreate,
    collection: Collection = Depends(get_comments_collection),
    current_user: dict = Depends(get_firebase_user),
) -> CommentResponse:
    """Create a comment. user_id is sourced from the Firebase token — never from the body."""
    user_id: str = current_user.get("user_id")
    await _verify_resource_visible(payload.resource_type, payload.resource_id, user_id)

    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user_id,
        "resource_type": payload.resource_type,
        "resource_id": payload.resource_id,
        "anchor": payload.anchor.model_dump(),
        "body": payload.body,
        "resolved": False,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "deleted_by": None,
    }
    result = await collection.insert_one(doc)
    created = await collection.find_one({"_id": result.inserted_id})
    if not created:
        logger.error(f"Comment inserted (id={result.inserted_id}) but not found on read-back")
        raise HTTPException(status_code=500, detail="Failed to create comment")
    return _to_response(created)


@router.get("", response_model=List[CommentResponse])
async def list_comments(
    resource_type: ResourceType = Query(...),
    resource_id: str = Query(...),
    collection: Collection = Depends(get_comments_collection),
    current_user: dict = Depends(get_firebase_user),
) -> List[CommentResponse]:
    """
    List the authenticated user's own comments on a resource.

    CRITICAL: filtered by resource_type + resource_id + user_id together — see
    module docstring. Never drop the user_id clause, even for "shared" resources.
    """
    user_id: str = current_user.get("user_id")
    cursor = collection.find(
        {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "user_id": user_id,
            "deleted_at": None,
        }
    )
    docs = await cursor.to_list(length=200)  # bounded — never unbounded
    return [_to_response(doc) for doc in docs]


@router.patch("/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: str,
    payload: CommentUpdate,
    collection: Collection = Depends(get_comments_collection),
    existing: dict = Depends(require_ownership(get_comments_collection, "comment_id")),
) -> CommentResponse:
    """Partially update a comment's body and/or resolved flag. Strict-ownership only."""
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No data provided for update")

    update_data["updated_at"] = datetime.now(timezone.utc)

    obj_id = ObjectId(existing["_id"]) if len(existing["_id"]) == 24 else existing["_id"]
    result = await collection.update_one({"_id": obj_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found")

    updated = await collection.find_one({"_id": obj_id})
    if not updated:
        raise HTTPException(status_code=500, detail="Error fetching updated comment")
    return _to_response(updated)


@router.delete("/{comment_id}", status_code=204)
async def delete_comment(
    collection: Collection = Depends(get_comments_collection),
    existing: dict = Depends(require_ownership(get_comments_collection, "comment_id")),
) -> None:
    """Soft-delete a comment (sets deleted_at/deleted_by). Never hard-deletes."""
    now = datetime.now(timezone.utc)
    obj_id = ObjectId(existing["_id"]) if len(existing["_id"]) == 24 else existing["_id"]
    result = await collection.update_one(
        {"_id": obj_id},
        {"$set": {"deleted_at": now, "deleted_by": existing.get("user_id"), "updated_at": now}},
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Comment not found")
    return None
