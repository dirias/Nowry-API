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

from app.config.entitlements import access_fields, resolve_access
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


#: ADR-085: an auto-provisioned account's name was derived from the token; one a person picked is chosen.
USERNAME_DERIVED = "derived"
USERNAME_CHOSEN = "chosen"


async def _apply_chosen_username(existing_user: dict, requested: str | None, firebase_uid: str) -> str:
    """Set the sign-up's chosen name on an account that still carries a derived one; else change nothing.

    The update is conditional on the marker, so a request that loses a race,
    or comes from a later sign-in, cannot rename the account (ADR-085).
    """
    current: str = existing_user.get("username")
    if existing_user.get("username_source") != USERNAME_DERIVED or not requested:
        return current
    now = datetime.now(timezone.utc)
    for candidate in (requested, f"{requested}-{firebase_uid[:6]}"[:USERNAME_MAX_LENGTH]):
        try:
            result = await users_collection.update_one(
                {"_id": existing_user["_id"], "username_source": USERNAME_DERIVED},
                {"$set": {"username": candidate, "username_source": USERNAME_CHOSEN, "updated_at": now}},
            )
        except DuplicateKeyError:
            continue  # another account owns it: the clash suffix (NAME-002 will change it)
        if result.modified_count:
            return candidate
        fresh = await users_collection.find_one({"_id": existing_user["_id"]}, {"username": 1})
        return (fresh or {}).get("username", current)
    return current


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

    # The body's email is the caller's word; the token's is Firebase's. The
    # lookup below matches on email, so a mismatch let a signed-in caller read
    # another account's ids and rename it (QA-008).
    token_email: str = (firebase_user.get("email") or "").strip().lower()
    if token_email != request.email.strip().lower():
        raise HTTPException(
            status_code=400,
            detail="Email mismatch"
        )
    
    # Check if user already exists
    # ADR-081: a deleted account is not "existing"; its identity was released.
    existing_user = await users_collection.find_one({
        "$or": [
            {"firebase_uid": request.firebase_uid},
            {"email": request.email}
        ],
        "deleted_at": None,
    })
    
    if existing_user:
        # The expected race with get_firebase_user's auto-provisioning: the
        # dependency ran before this handler and may have written the account
        # with a name derived from the token. Only that account, marked
        # `derived`, takes the name the sign-up chose. Any other account keeps
        # its username: a sign-in never renames anyone (ADR-085, FR-035).
        final_username = await _apply_chosen_username(existing_user, request.username, request.firebase_uid)

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
        "username_source": USERNAME_CHOSEN,  # ADR-085
        "photo_url": request.photo_url,
        "role": "user",
        "subscription": {
            "tier": SubscriptionTier.FREE,
            "status": "active"
        },
        "wizard_completed": False,
        # ADR-076: what Firebase says of the email today, and how the account was made.
        "email_verified": bool(firebase_user.get("email_verified")),
        "signup_provider": firebase_user.get("sign_in_provider"),
        "preferences": {
            "pet": {
                # ROAM-002, FR-037: the study buddy is on from the first screen,
                # resting in one place (roaming is off, FR-036). The person can
                # turn it off in companion settings; that choice is kept.
                "pet_active": True,
                "pet_revealed": True,
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
    
    # The account the dependency resolved (ADR-083), not a second lookup by uid.
    user_id = firebase_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id), "deleted_at": None}) if user_id and ObjectId.is_valid(user_id) else None
    
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
        "subscription": {
            **(user.get("subscription") or {"status": "active"}),
            **access_fields(resolve_access(user)),  # ADR-079
        },
    }
