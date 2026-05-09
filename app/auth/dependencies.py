from fastapi import Request, HTTPException, Depends
from bson import ObjectId
from typing import Callable
from pymongo.collection import Collection
from datetime import datetime, timezone

from app.auth.firebase_auth import get_firebase_user
from app.config.database import users_collection

def require_ownership(get_collection_dependency: Callable, id_param_name: str = "id"):
    """
    Returns a FastAPI dependency that verifies resource ownership.
    Ensures the document exists and its user_id matches the authenticated user.
    Uses the injected collection to query the DB dynamically.
    """
    async def _dependency(
        request: Request,
        collection: Collection = Depends(get_collection_dependency),
        current_user: dict = Depends(get_firebase_user)
    ) -> dict:
        resource_id = request.path_params.get(id_param_name)
        if not resource_id:
            raise HTTPException(status_code=400, detail=f"Missing {id_param_name} parameter in path")
            
        try:
            obj_id = ObjectId(resource_id)
        except Exception:
            obj_id = resource_id
            
        doc = await collection.find_one({"_id": obj_id})
        
        if not doc:
            raise HTTPException(status_code=404, detail="Resource not found")
            
        user_id = current_user.get("user_id")
        if doc.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Not authorized to access this resource")
            
        # Format MongoDB IDs for Pydantic
        doc["_id"] = str(doc["_id"])
        if "id" not in doc:
            doc["id"] = doc["_id"]
            
        return doc
        
    return _dependency

async def require_admin(
    current_user: dict = Depends(get_firebase_user)
) -> dict:
    """
    Verifies that the current user has is_admin=true in their MongoDB user document.
    Raises HTTPException(403) if is_admin is missing or False.

    Usage in route:
      @router.get("/admin/reports")
      async def list_reports(current_user: dict = Depends(require_admin)):
          # current_user is guaranteed to have is_admin=true
    """
    if not current_user.get("is_admin"):
        raise HTTPException(
            status_code=403,
            detail="Admin access required"
        )
    return current_user


async def track_ai_usage(
    current_user: dict = Depends(get_firebase_user),
) -> dict:
    """
    Increment the user's monthly AI usage counter by +1 per API call (D-09).
    Returns the updated user document. Does NOT enforce limits — enforcement is Phase 4.
    """
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)
    user = await users_collection.find_one_and_update(
        {"_id": ObjectId(user_id)},
        {
            "$inc": {"subscription.ai_usage_count": 1},
            "$set": {"subscription.last_ai_usage_at": now},
        },
        return_document=True,
        upsert=False,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def get_subscription_tier(
    current_user: dict = Depends(get_firebase_user),
) -> str:
    """Returns the user's current subscription tier as a string: 'free', 'plus', or 'pro'."""
    user_id = current_user.get("user_id")
    user = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"subscription.tier": 1},
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user.get("subscription", {}).get("tier", "free")


def require_public_or_ownership(get_collection_dependency: Callable, id_param_name: str = "id"):
    """
    Returns a FastAPI dependency that allows access if resource is public
    or belongs to the authenticated user.
    """
    async def _dependency(
        request: Request,
        collection: Collection = Depends(get_collection_dependency),
        current_user: dict = Depends(get_firebase_user) # Can be optional auth? If it's pure public, we would use optional_auth
    ) -> dict:
        resource_id = request.path_params.get(id_param_name)
        if not resource_id:
            raise HTTPException(status_code=400, detail=f"Missing {id_param_name} parameter in path")
            
        try:
            obj_id = ObjectId(resource_id)
        except Exception:
            obj_id = resource_id
            
        doc = await collection.find_one({"_id": obj_id})
        
        if not doc:
            raise HTTPException(status_code=404, detail="Resource not found")
            
        user_id = current_user.get("user_id") if current_user else None
        
        is_public = doc.get("is_public", False)
        if doc.get("user_id") != user_id and not is_public:
            raise HTTPException(status_code=403, detail="Not authorized to access this resource")
            
        doc["_id"] = str(doc["_id"])
        if "id" not in doc:
            doc["id"] = doc["_id"]
            
        return doc
        
    return _dependency
