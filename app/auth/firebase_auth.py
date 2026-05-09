"""
Firebase Authentication Module
Handles Firebase token validation and user authentication with caching
"""

from fastapi import HTTPException, Header, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config.firebase_config import verify_firebase_token
from app.config.subscription_plans import SubscriptionTier
from functools import lru_cache
from typing import Optional
from datetime import datetime, timezone
import time

# Simple in-memory cache for validated tokens (UID -> {token_data, expiry})
_token_cache = {}
_CACHE_TTL = 300  # 5 minutes cache

# HTTP Bearer for optional auth
security = HTTPBearer(auto_error=False)


def _get_cached_token(token: str) -> dict | None:
    """Check if token is in cache and not expired"""
    if token in _token_cache:
        cached_data, expiry = _token_cache[token]
        if time.time() < expiry:
            return cached_data
        else:
            # Expired, remove from cache
            del _token_cache[token]
    return None


def _cache_token(token: str, token_data: dict):
    """Cache validated token data"""
    _token_cache[token] = (token_data, time.time() + _CACHE_TTL)
    
    # Simple cache cleanup - remove old entries if cache gets too large
    if len(_token_cache) > 1000:
        current_time = time.time()
        expired_keys = [k for k, (_, exp) in _token_cache.items() if current_time >= exp]
        for k in expired_keys:
            del _token_cache[k]


async def get_firebase_user(request: Request) -> dict:
    """
    Extract and validate Firebase ID token from Authorization header or cookie
    
    Args:
        request: FastAPI Request object
        
    Returns:
        dict: Decoded Firebase token with user claims
        
    Raises:
        HTTPException: If token is missing or invalid
    """
    # Try to get token from Authorization header first
    auth_header = request.headers.get("Authorization")
    token = None
    
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "")
    
    # Fallback to cookie
    if not token:
        token = request.cookies.get("firebase_token")
    
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. No Firebase token found."
        )
    
    # Check cache first
    cached_data = _get_cached_token(token)
    if cached_data:
        return cached_data
    
    try:
        # Verify token with Firebase Admin SDK (network call - slow)
        decoded_token = verify_firebase_token(token)
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid Firebase token: {str(e)}"
        )

    try:
        token_data = {
            "firebase_uid": decoded_token.get("uid"),
            "email": decoded_token.get("email"),
            "email_verified": decoded_token.get("email_verified", False),
            "name": decoded_token.get("name"),
            "picture": decoded_token.get("picture"),
        }

        # --- Resolve MongoDB user_id ---
        # Many endpoints expect "user_id" (MongoDB _id) to be present.
        from app.config.database import users_collection

        # Search without filtering on deleted_at so soft-deleted docs are found too.
        # Try by firebase_uid first, then fall back to email (covers the case where
        # Firebase deleted the user and re-issued a new UID on re-registration with
        # the same Google account — same email, different UID).
        user = await users_collection.find_one({"firebase_uid": token_data["firebase_uid"]})
        if not user and token_data.get("email"):
            user = await users_collection.find_one({"email": token_data["email"]})

        if user and not user.get("deleted_at"):
            # Normal path: active user doc found.
            token_data["user_id"] = str(user["_id"])
        elif user and user.get("deleted_at"):
            # Soft-deleted account: the user deleted their account but is signing
            # back in with the same Google identity (same or new Firebase UID but
            # same email).  Reactivate the existing document and update the
            # firebase_uid to the current one — do NOT insert a new doc, which
            # would hit the unique index on firebase_uid/email and raise a
            # DuplicateKeyError (causing a 500).
            now = datetime.now(timezone.utc)
            display_name: str = token_data.get("name") or ""
            await users_collection.update_one(
                {"_id": user["_id"]},
                {
                    "$unset": {"deleted_at": "", "deleted_by": ""},
                    "$set": {
                        # Always sync firebase_uid in case it changed (new Firebase
                        # account after hard-delete on the Firebase side).
                        "firebase_uid": token_data["firebase_uid"],
                        "photo_url": token_data.get("picture") or user.get("photo_url"),
                        "full_name": display_name or user.get("full_name", ""),
                        "wizard_completed": False,
                        "updated_at": now,
                    },
                },
            )
            token_data["user_id"] = str(user["_id"])
        else:
            # Valid Firebase token but no MongoDB document — first-time sign-in
            # via Google before /auth/register is called.
            # Auto-create a minimal user document so the request succeeds and
            # the frontend can drive onboarding (wizard) normally.
            display_name: str = token_data.get("name") or ""
            email: str = token_data.get("email") or ""
            username: str = display_name or (email.split("@")[0] if email else "user")
            now = datetime.now(timezone.utc)
            new_user_doc = {
                "firebase_uid": token_data["firebase_uid"],
                "email": email,
                "username": username,
                "full_name": display_name or username,
                "photo_url": token_data.get("picture"),
                "role": "user",
                "subscription": {
                    "tier": SubscriptionTier.FREE.value,
                    "status": "active",
                },
                "wizard_completed": False,
                "created_at": now,
                "updated_at": now,
            }
            result = await users_collection.insert_one(new_user_doc)
            token_data["user_id"] = str(result.inserted_id)

        # uid is an alias for firebase_uid (Firebase authentication identifier only)
        # NEVER use uid for MongoDB storage — always use user_id (MongoDB ObjectId string)
        token_data["uid"] = token_data.get("firebase_uid")

        # Cache the validated token AFTER MongoDB data is fully joined
        _cache_token(token, token_data)

        return token_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to resolve user profile: {str(e)}"
        )


async def get_current_user(request: Request) -> dict:
    """
    Get authenticated user (required).
    Alias for get_firebase_user with uid compatibility.
    """
    user_data = await get_firebase_user(request)
    return user_data


async def optional_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[dict]:
    """
    Get authenticated user (optional - returns None if not logged in).
    Used for public endpoints that work for both authenticated and anonymous users.
    
    Args:
        request: FastAPI Request object
        credentials: Optional HTTP Bearer credentials
        
    Returns:
        dict | None: User data if authenticated, None otherwise
    """
    if not credentials:
        return None
    
    try:
        # Extract token
        token = credentials.credentials
        
        # Check cache first
        cached_data = _get_cached_token(token)
        if cached_data:
            return cached_data
        
        # Verify token
        decoded_token = verify_firebase_token(token)
        
        token_data = {
            "firebase_uid": decoded_token.get("uid"),
            "uid": decoded_token.get("uid"),  # Alias
            "email": decoded_token.get("email"),
            "email_verified": decoded_token.get("email_verified", False),
            "name": decoded_token.get("name"),
            "picture": decoded_token.get("picture"),
        }
        
        # Fetch MongoDB user ID
        from app.config.database import users_collection
        user = await users_collection.find_one({"firebase_uid": token_data["firebase_uid"]})
        
        if user:
            token_data["user_id"] = str(user["_id"])
        
        # Cache it
        _cache_token(token, token_data)
        
        return token_data
    except:
        # Silent fail - user not logged in or invalid token
        return None
