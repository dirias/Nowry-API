"""
DEPRECATED Session Router
This router contains legacy authentication endpoints that are deprecated.
All new authentication should use Firebase Auth via /auth endpoints.

These endpoints are kept temporarily for backward compatibility but will be removed.
"""

from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import JSONResponse
import jwt
import bcrypt
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config.database import users_collection
from app.config.auth_config import SECRET_KEY, SECURE_COOKIE, SAMESITE_COOKIE

router = APIRouter(
    prefix="/session",
    tags=["sessions (DEPRECATED)"],
    responses={404: {"description": "Not found"}},
    deprecated=True  # Mark entire router as deprecated
)


from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login", deprecated=True)
@Limiter(key_func=get_remote_address).limit("5/minute")
async def login_deprecated(request: Request, credentials: LoginRequest):
    """
    DEPRECATED: Use Firebase Auth via /auth/login instead
    
    This endpoint uses legacy password storage and will be removed.
    """
    raise HTTPException(
        status_code=410,  # Gone
        detail="This endpoint is deprecated. Please use Firebase Authentication via /auth/login"
    )


@router.post("/logout", deprecated=True)
async def logout_deprecated():
    """
    DEPRECATED: Use Firebase Auth logout (client-side) instead
    
    Firebase handles logout on the client side via Firebase SDK.
    """
    raise HTTPException(
        status_code=410,
        detail="This endpoint is deprecated. Use Firebase Auth logout on client side"
    )


@router.post("/reset_password", deprecated=True)
async def reset_password_deprecated(email: str = Form(...), new_password: str = Form(...)):
    """
    DEPRECATED: Use Firebase Password Reset instead
    
    Password reset should be handled by Firebase via sendPasswordResetEmail()
    """
    raise HTTPException(
        status_code=410,
        detail="This endpoint is deprecated. Use Firebase sendPasswordResetEmail() instead"
    )
