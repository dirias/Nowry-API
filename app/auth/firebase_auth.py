"""
Firebase Authentication Module
Handles Firebase token validation and user authentication with caching
"""

from __future__ import annotations

from fastapi import HTTPException, Header, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pymongo.errors import DuplicateKeyError
from app.config.firebase_config import verify_firebase_token
from app.config.subscription_plans import SubscriptionTier
from functools import lru_cache
from typing import Optional
from datetime import datetime, timezone
import asyncio
import logging
import os
import time
import stripe as _stripe

# Configure Stripe at module level — never inside a request handler (RESEARCH.md Pattern 1)
_stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

logger = logging.getLogger(__name__)

def _first_of_next_month(dt: datetime) -> datetime:
    """Returns the first day of the next calendar month at 00:00:00 UTC."""
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    return dt.replace(month=dt.month + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


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

            # Create Stripe customer — wrapped in try/except so a Stripe failure
            # never blocks user creation (T-03-02-02 mitigation)
            stripe_customer_id = None
            try:
                customer = await asyncio.to_thread(
                    _stripe.Customer.create,
                    email=email,
                    metadata={"firebase_uid": token_data["firebase_uid"]},
                )
                stripe_customer_id = customer.id
            except Exception as _stripe_err:
                logger.warning("Stripe customer creation failed for new user: %s", _stripe_err)

            new_user_doc = {
                "firebase_uid": token_data["firebase_uid"],
                "email": email,
                "username": username,
                "full_name": display_name or username,
                "photo_url": token_data.get("picture"),
                "role": "user",
                "stripe_customer_id": stripe_customer_id,  # None if Stripe call failed
                "subscription": {
                    "tier": SubscriptionTier.FREE.value,
                    "status": "active",
                    "ai_usage_count": 0,
                    "ai_usage_reset_date": _first_of_next_month(now),
                    "next_billing_date": None,
                    "stripe_subscription_id": None,
                    "billing_interval": None,
                    "subscription_status_updated_at": now,
                },
                "wizard_completed": False,
                "created_at": now,
                "updated_at": now,
            }
            try:
                result = await users_collection.insert_one(new_user_doc)
                token_data["user_id"] = str(result.inserted_id)
            except DuplicateKeyError:
                # A unique index (firebase_uid/email/username) collided with an
                # already-existing active user document. This happens when:
                #   - two concurrent first-login requests race the insert, or
                #   - the token's uid/email no longer matches the stored doc
                #     (e.g. re-auth issued a new uid for the same email).
                # Recover instead of 500ing: re-fetch the real existing doc by
                # email (most reliable unique key here) and sync firebase_uid
                # onto it so this and future logins resolve correctly.
                existing = await users_collection.find_one({"email": email}) if email else None
                if not existing:
                    existing = await users_collection.find_one(
                        {"firebase_uid": token_data["firebase_uid"]}
                    )
                if existing:
                    if existing.get("firebase_uid") != token_data["firebase_uid"]:
                        await users_collection.update_one(
                            {"_id": existing["_id"]},
                            {
                                "$set": {
                                    "firebase_uid": token_data["firebase_uid"],
                                    "updated_at": now,
                                }
                            },
                        )
                    token_data["user_id"] = str(existing["_id"])
                else:
                    # No matching doc by email/uid — this was a genuine
                    # username collision between two different accounts that
                    # happen to share a display name. Disambiguate and retry
                    # once rather than 500ing on a brand-new sign-up.
                    new_user_doc["username"] = f"{username}-{token_data['firebase_uid'][:6]}"
                    try:
                        result = await users_collection.insert_one(new_user_doc)
                        token_data["user_id"] = str(result.inserted_id)
                    except DuplicateKeyError:
                        # Still colliding (e.g. firebase_uid/email race resolved
                        # elsewhere in the meantime) — surface the failure.
                        raise

        # uid is an alias for firebase_uid (Firebase authentication identifier only)
        # NEVER use uid for MongoDB storage — always use user_id (MongoDB ObjectId string)
        token_data["uid"] = token_data.get("firebase_uid")

        # Cache the validated token AFTER MongoDB data is fully joined
        _cache_token(token, token_data)

        return token_data
    except HTTPException:
        raise
    except Exception as e:
        # Log the full traceback server-side — the client only ever sees the
        # generic detail string below, but without this, an unhandled error
        # here is invisible in the access logs (no traceback is ever printed
        # for a deliberately-raised HTTPException).
        logger.exception("get_firebase_user: failed to resolve user profile")
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
