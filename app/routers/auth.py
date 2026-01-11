"""
Authentication Router for Firebase Integration
Handles user registration and login with Firebase Authentication
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from bson import ObjectId
from datetime import datetime

from app.config.database import users_collection
from app.auth.firebase_auth import get_firebase_user
from app.config.subscription_plans import SubscriptionTier

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
    responses={404: {"description": "Not found"}},
)


class RegisterRequest(BaseModel):
    firebase_uid: str
    email: EmailStr
    username: str


class LoginRequest(BaseModel):
    firebase_uid: str
    email: EmailStr


@router.post("/register")
async def register_user(
    request: RegisterRequest,
    firebase_user: dict = Depends(get_firebase_user)
):
    """
    Register a new user after Firebase authentication
    
    Flow:
    1. User signs up with Firebase (frontend)
    2. Firebase creates user and returns UID
    3. Frontend calls this endpoint with Firebase UID
    4. Backend creates user record in MongoDB with Firebase UID reference
    """
    
    # Verify Firebase UID matches the token
    if firebase_user.get("firebase_uid") != request.firebase_uid:
        raise HTTPException(
            status_code=400,
            detail="Firebase UID mismatch"
        )
    
    # Check if user already exists
    existing_user = await users_collection.find_one({
        "$or": [
            {"firebase_uid": request.firebase_uid},
            {"email": request.email}
        ]
    })
    
    if existing_user:
        # User already exists, return existing user data
        return {
            "message": "User already exists",
            "user_id": str(existing_user["_id"]),
            "firebase_uid": existing_user.get("firebase_uid"),
            "email": existing_user.get("email"),
            "username": existing_user.get("username"),
            "wizard_completed": existing_user.get("wizard_completed", False)
        }
    
    # Create new user in MongoDB
    user_doc = {
        "firebase_uid": request.firebase_uid,
        "email": request.email,
        "username": request.username,
        "role": "user",
        "subscription": {
            "tier": SubscriptionTier.FREE,
            "status": "active"
        },
        "wizard_completed": False,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    result = await users_collection.insert_one(user_doc)
    user_doc["_id"] = str(result.inserted_id)
    
    return {
        "message": "User registered successfully",
        "user_id": str(result.inserted_id),
        "firebase_uid": request.firebase_uid,
        "email": request.email,
        "username": request.username,
        "wizard_completed": False
    }


@router.post("/login")
async def login_user(
    request: LoginRequest,
    firebase_user: dict = Depends(get_firebase_user)
):
    """
    Login user with Firebase authentication
    
    Flow:
    1. User signs in with Firebase (frontend)
    2. Firebase validates credentials and returns ID token
    3. Frontend calls this endpoint with Firebase token
    4. Backend validates token and returns user data from MongoDB
    """
    
    # Verify Firebase UID matches the token
    if firebase_user.get("firebase_uid") != request.firebase_uid:
        raise HTTPException(
            status_code=400,
            detail="Firebase UID mismatch"
        )
    
    # Find user in MongoDB by Firebase UID
    user = await users_collection.find_one({"firebase_uid": request.firebase_uid})
    
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found. Please register first."
        )
    
    return {
        "message": "Login successful",
        "user_id": str(user["_id"]),
        "firebase_uid": user.get("firebase_uid"),
        "email": user.get("email"),
        "username": user.get("username"),
        "role": user.get("role", "user"),
        "wizard_completed": user.get("wizard_completed", False),
        "subscription": user.get("subscription", {"tier": "free", "status": "active"})
    }
