"""
Authentication Router for Firebase Integration
Handles user registration and login with Firebase Authentication
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from bson import ObjectId
from datetime import datetime, timezone
from pymongo.errors import DuplicateKeyError

from app.config.database import users_collection
from app.auth.firebase_auth import get_firebase_user
from app.config.subscription_plans import SubscriptionTier
from app.models.common import UserAuthResponse
from app.models.User import USERNAME_MAX_LENGTH, USERNAME_MIN_LENGTH, USERNAME_PATTERN

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
    responses={404: {"description": "Not found"}},
)


class RegisterRequest(BaseModel):
    firebase_uid: str
    email: EmailStr
    # Same format rule enforced on edit via ProfilePatchRequest.username
    # (app/routers/users.py) — shared constants imported from app.models.User
    # so the two can never drift apart.
    username: str = Field(
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        pattern=USERNAME_PATTERN,
    )
    photo_url: str | None = None


class LoginRequest(BaseModel):
    firebase_uid: str
    email: EmailStr


@router.post("/register", response_model=UserAuthResponse)
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
        # User already exists — this is the expected race with
        # get_firebase_user's auto-provisioning: the dependency above ran
        # BEFORE this handler body and may have already inserted a doc with
        # a username derived from the Firebase token (e.g. the email prefix,
        # if the ID token's `name` claim wasn't populated yet). If the
        # username the signup form actually submitted differs from what got
        # auto-provisioned, reconcile it now instead of silently discarding
        # the value the user actually typed.
        final_username = existing_user.get("username")
        if request.username and request.username != final_username:
            now = datetime.now(timezone.utc)
            try:
                await users_collection.update_one(
                    {"_id": existing_user["_id"]},
                    {"$set": {"username": request.username, "updated_at": now}},
                )
                final_username = request.username
            except DuplicateKeyError:
                # Another account already owns this exact username — disambiguate
                # the same way firebase_auth.py's auto-provision collision
                # handling does, rather than losing the reconciliation entirely.
                # Truncated to USERNAME_MAX_LENGTH: request.username is already
                # validated at up to 30 chars, so appending "-{uid[:6]}" could
                # otherwise exceed the same limit we just started enforcing.
                disambiguated = f"{request.username}-{request.firebase_uid[:6]}"[:USERNAME_MAX_LENGTH]
                await users_collection.update_one(
                    {"_id": existing_user["_id"]},
                    {"$set": {"username": disambiguated, "updated_at": now}},
                )
                final_username = disambiguated

        return {
            "message": "User already exists",
            "user_id": str(existing_user["_id"]),
            "firebase_uid": existing_user.get("firebase_uid"),
            "email": existing_user.get("email"),
            "username": final_username,
            "photo_url": existing_user.get("photo_url"),
            "wizard_completed": existing_user.get("wizard_completed", False)
        }
    
    # Create new user in MongoDB
    user_doc = {
        "firebase_uid": request.firebase_uid,
        "email": request.email,
        "username": request.username,
        "photo_url": request.photo_url,
        "role": "user",
        "subscription": {
            "tier": SubscriptionTier.FREE,
            "status": "active"
        },
        "wizard_completed": False,
        "preferences": {
            "pet": {
                # New accounts start with the pet inactive; it auto-activates
                # the first time the user completes a study session, via
                # POST /agent/pet/reveal. This is the only place `False` is
                # ever written for these fields — legacy docs and the
                # PetPreferencesResponse model both default to True so
                # existing accounts keep behaving as already active.
                "pet_active": False,
                "pet_revealed": False,
            }
        },
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
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


@router.post("/login", response_model=UserAuthResponse)
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
