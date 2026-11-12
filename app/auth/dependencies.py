from fastapi import Request, HTTPException, Depends
from bson import ObjectId
from typing import Callable
from pymongo.collection import Collection
from datetime import datetime, timezone

from app.auth.firebase_auth import get_firebase_user
from app.config.database import users_collection
from app.config.entitlements import ACCESS_PROJECTION, Access, access_limit, resolve_access

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
            
        # Exclude soft-deleted resources — every other query in this codebase
        # (list/get/aggregate) already filters deleted_at: None; this dependency
        # didn't, letting a user still fetch/PATCH/re-grade cards or decks they'd
        # already soft-deleted via the DELETE endpoint (see 32-REVIEW.md WR-03).
        doc = await collection.find_one({"_id": obj_id, "deleted_at": None})
        
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
    Verifies that the current user's MongoDB document says ``is_admin: true``.

    The flag is read from the account on every call (INVITE-001, ADR-077).
    ``current_user`` is built from the Firebase token and never carries it, and
    a token is cached for minutes: reading the document means a flag taken away
    stops working at the next request. Anything but a stored ``True`` is 403.

    Usage in route:
      @router.get("/admin/reports")
      async def list_reports(current_user: dict = Depends(require_admin)):
          # the account behind current_user has is_admin=true
    """
    refusal = HTTPException(status_code=403, detail="Admin access required")
    user_id = current_user.get("user_id")
    if not user_id or not ObjectId.is_valid(user_id):
        raise refusal
    account = await users_collection.find_one(
        {"_id": ObjectId(user_id), "deleted_at": None}, {"is_admin": 1}
    )
    if not account or account.get("is_admin") is not True:
        raise refusal
    return current_user


AI_LIMIT_REACHED_CODE = "ai_limit_reached"


def first_of_next_month(dt: datetime) -> datetime:
    """The first day of the next calendar month at 00:00 UTC (the usage window's end)."""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return dt.replace(month=dt.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


async def _roll_usage_window(user_id: str, subscription: dict, now: datetime) -> datetime:
    """Reset the monthly counter when its window has passed; return the window's end."""
    reset_at = subscription.get("ai_usage_reset_date")
    if isinstance(reset_at, datetime) and reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    if isinstance(reset_at, datetime) and now < reset_at:
        return reset_at
    next_reset = first_of_next_month(now)
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"subscription.ai_usage_count": 0, "subscription.ai_usage_reset_date": next_reset}},
    )
    return next_reset


async def track_ai_usage(
    current_user: dict = Depends(get_firebase_user),
) -> dict:
    """
    Count one AI generation call against the user's monthly ceiling (GTM-003, ADR-041).

    The ceiling is `ai_calls_per_month` of the plan in effect (-1 for none;
    ADR-079: a tester's own cap). It is taken once per request, before any
    model is asked, so an answer that came from the fallback counts once. The window is
    the calendar month, kept in `subscription.ai_usage_reset_date` and rolled
    here when it has passed. The increment is conditional on being under the
    ceiling, so a refused call is never counted, and the refusal is
    `429 {"code": "ai_limit_reached", "limit", "resets_at"}`.
    """
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)
    current = await users_collection.find_one({"_id": ObjectId(user_id)}, ACCESS_PROJECTION)
    if not current:
        raise HTTPException(status_code=404, detail="User not found")
    subscription: dict = current.get("subscription") or {}
    # ADR-079: the ceiling is the one of the plan in effect, a tester's cap included.
    limit: int = access_limit(resolve_access(current, now), "ai_calls_per_month")
    resets_at = await _roll_usage_window(user_id, subscription, now)

    query: dict = {"_id": ObjectId(user_id)}
    if limit != -1:
        query["$or"] = [
            {"subscription.ai_usage_count": {"$lt": limit}},
            {"subscription.ai_usage_count": {"$exists": False}},
        ]
    user = await users_collection.find_one_and_update(
        query,
        {
            "$inc": {"subscription.ai_usage_count": 1},
            "$set": {"subscription.last_ai_usage_at": now},
        },
        return_document=True,
        upsert=False,
    )
    if not user:
        raise HTTPException(
            status_code=429,
            detail={"code": AI_LIMIT_REACHED_CODE, "limit": limit, "resets_at": resets_at.isoformat()},
        )
    # Re-inject user_id (Firebase UID string) — MongoDB doc has _id/firebase_uid
    # but not user_id. Endpoints call current_user.get("user_id") for ownership checks.
    user["user_id"] = current_user.get("user_id")
    return user


async def get_access(
    current_user: dict = Depends(get_firebase_user),
) -> Access:
    """The plan this account is answered as, and why (ADR-079)."""
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)}, ACCESS_PROJECTION)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return resolve_access(user)


async def get_subscription_tier(
    current_user: dict = Depends(get_firebase_user),
) -> str:
    """The plan in effect as a string: 'free', 'plus', or 'pro' (ADR-079)."""
    return (await get_access(current_user)).tier


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
            
        # Exclude soft-deleted resources — every other query in this codebase
        # (list/get/aggregate) already filters deleted_at: None; this dependency
        # didn't, letting a user still fetch/PATCH/re-grade cards or decks they'd
        # already soft-deleted via the DELETE endpoint (see 32-REVIEW.md WR-03).
        doc = await collection.find_one({"_id": obj_id, "deleted_at": None})
        
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
