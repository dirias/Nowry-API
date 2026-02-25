"""
Firebase Authentication Module
Handles Firebase token validation and user authentication with caching
"""

from fastapi import HTTPException, Header, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config.firebase_config import verify_firebase_token
from functools import lru_cache
from typing import Optional
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
        
        token_data = {
            "firebase_uid": decoded_token.get("uid"),
            "email": decoded_token.get("email"),
            "email_verified": decoded_token.get("email_verified", False),
            "name": decoded_token.get("name"),
            "picture": decoded_token.get("picture"),
        }
        
        # Cache the validated token
        _cache_token(token, token_data)
        
        # --- NEW: Fetch MongoDB User ID ---
        # Many endpoints expect "user_id" (MongoDB _id) to be present
        from app.config.database import users_collection
        
        # We use a simple in-memory cache for user lookups to avoid DB hit every request
        # In production, use Redis or similar
        user = await users_collection.find_one({"firebase_uid": token_data["firebase_uid"]})
        
        if user:
            token_data["user_id"] = str(user["_id"])
        else:
            # Should not happen for registered users, but handle gracefully
            token_data["user_id"] = None
        
        # Add uid alias for compatibility
        token_data["uid"] = token_data.get("firebase_uid") or token_data.get("user_id")
            
        return token_data
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid Firebase token: {str(e)}"
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
