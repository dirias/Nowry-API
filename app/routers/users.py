"""
User Profile Router
Handles user profile management, settings, and preferences
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pymongo.collection import Collection
from bson import ObjectId
from datetime import datetime, timedelta
from typing import Optional, List
import bcrypt
import base64
import secrets
import asyncio
from pydantic import BaseModel, ConfigDict, EmailStr, validator

from app.models.User import User
from app.config.database import (
    users_collection,
    study_cards_collection,
    books_collection,
    decks_collection,
)
from app.auth.firebase_auth import get_firebase_user

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={404: {"description": "Not found"}},
)


class UserMeResponse(BaseModel):
    id: str
    firebase_uid: str
    username: str
    email: str
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    subscription: dict
    preferences: dict
    created_at: Optional[datetime] = None
    wizard_completed: bool


@router.get("/me", response_model=UserMeResponse)
async def get_current_user_profile(
    current_user: dict = Depends(get_firebase_user),
) -> UserMeResponse:
    """Get current user profile"""
    # With Firebase auth, we get firebase_uid from the token
    firebase_uid = current_user.get("firebase_uid")

    if not firebase_uid:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    # Find user by firebase_uid
    user = await users_collection.find_one({"firebase_uid": firebase_uid})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Return safe user data
    return UserMeResponse(
        id=str(user["_id"]),
        firebase_uid=user.get("firebase_uid"),
        username=user.get("username"),
        email=user.get("email"),
        full_name=user.get("full_name"),
        avatar_url=user.get("avatar_url"),
        role=user.get("role", "user"),
        subscription=user.get("subscription", {}),
        preferences=user.get("preferences", {}),
        created_at=user.get("created_at"),
        wizard_completed=user.get("wizard_completed", False),
    )


# Pydantic Models
class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    bio: Optional[str] = None


class ProfileResponse(BaseModel):
    id: str
    username: str
    email: str
    full_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None
    created_at: datetime
    subscription: dict
    stats: dict
    notification_preferences: dict
    preferences: dict
    two_factor_enabled: bool = False


class ProfilePatchRequest(BaseModel):
    full_name: Optional[str] = None
    bio: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ProfilePatchResponse(BaseModel):
    message: str
    full_name: Optional[str] = None
    bio: Optional[str] = None
    updated_at: datetime


class PasswordChange(BaseModel):
    current_password: str
    new_password: str

    @validator("new_password")
    def validate_password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        return v


class NotificationPreferences(BaseModel):
    email_digest: Optional[bool] = None
    study_reminders: Optional[bool] = None
    news_updates: Optional[bool] = None
    marketing: Optional[bool] = None


class FavoriteArticle(BaseModel):
    url: str
    title: str
    description: Optional[str] = None
    urlToImage: Optional[str] = None
    category: Optional[str] = None


class UserPreferences(BaseModel):
    interests: Optional[List[str]] = None
    theme_color: Optional[str] = None
    language: Optional[str] = None
    pomodoro_work_minutes: Optional[int] = None
    pomodoro_short_break_minutes: Optional[int] = None
    pomodoro_long_break_minutes: Optional[int] = None
    pomodoro_auto_start: Optional[bool] = None
    pomodoro_enabled: Optional[bool] = None
    favorite_news: Optional[List[FavoriteArticle]] = None




from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier


# Helper Functions
async def get_user_stats(user_id: str) -> dict:
    """Calculate user statistics"""
    try:
        # Convert user_id to ObjectId for collections that use PyObjectId/ObjectId
        try:
            user_oid = ObjectId(user_id)
        except Exception:
            user_oid = user_id  # Fallback if not a valid ObjectId

        # Parallelize independent count queries
        (
            total_cards,
            flashcards_count,
            reviewed_cards,
            books_created,
            quiz_questions,
            visual_diagrams
        ) = await asyncio.gather(
            study_cards_collection.count_documents({"user_id": user_oid}),
            study_cards_collection.count_documents({"user_id": user_oid, "card_type": {"$in": [None, "flashcard"]}}),
            study_cards_collection.count_documents({"user_id": user_oid, "last_reviewed": {"$exists": True}}),
            books_collection.count_documents({"user_id": user_id}),
            study_cards_collection.count_documents({"user_id": user_oid, "card_type": "quiz"}),
            study_cards_collection.count_documents({"user_id": user_oid, "card_type": "visual"})
        )

        # Calculate study streak efficiently (fetch last 365 days of reviews at once)
        one_year_ago = datetime.utcnow() - timedelta(days=365)
        reviewed_cards_cursor = study_cards_collection.find(
            {
                "user_id": user_oid,
                "last_reviewed": {"$gt": one_year_ago}
            },
            {"last_reviewed": 1}
        )
        
        # Collect unique dates locally
        reviewed_dates = set()
        async for card in reviewed_cards_cursor:
            if "last_reviewed" in card and card["last_reviewed"]:
                reviewed_dates.add(card["last_reviewed"].date())
                
        # Calculate streak
        today = datetime.utcnow().date()
        streak = 0
        check_date = today
        
        while check_date in reviewed_dates:
            streak += 1
            check_date -= timedelta(days=1)

        return {
            "total_cards": total_cards,
            "flashcards_count": flashcards_count,
            "reviewed_cards": reviewed_cards,
            "books_created": books_created,
            "study_streak": streak,
            "quiz_questions": quiz_questions,
            "visual_diagrams": visual_diagrams,
            "ai_generations_month": 0,  # TODO: Track monthly AI usage
        }
    except Exception as e:
        print(f"Error calculating user stats: {e}")
        # Return default zero stats on error
        return {
            "total_cards": 0,
            "flashcards_count": 0,
            "reviewed_cards": 0,
            "books_created": 0,
            "study_streak": 0,
            "quiz_questions": 0,
            "visual_diagrams": 0,
            "ai_generations_month": 0,
        }


# Routes
@router.get("/profile", response_model=ProfileResponse)
async def get_profile(current_user: dict = Depends(get_firebase_user)) -> ProfileResponse:
    """Get current user's profile"""
    user_id = current_user.get("user_id")

    try:
        user = await users_collection.find_one({"_id": ObjectId(user_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid User ID")

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get user stats (usage)
    stats = await get_user_stats(user_id)

    # Get subscription info
    stored_sub = user.get("subscription", {"tier": "free", "status": "active"})
    tier_key = stored_sub.get("tier", "free")

    # Map stored tier to plan configuration
    plan_details = SUBSCRIPTION_PLANS.get(
        tier_key, SUBSCRIPTION_PLANS[SubscriptionTier.FREE]
    )

    # Construct full subscription object
    subscription = {
        "tier": tier_key,
        "status": stored_sub.get("status", "active"),
        "name": plan_details["name"],
        "features": plan_details["features"],
        "limits": plan_details["limits"],
        "usage": {
            "books": stats["books_created"],
            "flashcards": stats["flashcards_count"],
            "quiz_questions": stats["quiz_questions"],
            "visual_diagrams": stats["visual_diagrams"],
            "ai_generations": stats["ai_generations_month"],
        },
    }

    # Get notification preferences
    notification_preferences = user.get(
        "notification_preferences",
        {
            "email_digest": True,
            "study_reminders": True,
            "news_updates": False,
            "marketing": False,
        },
    )

    # Get general preferences
    preferences = user.get(
        "preferences",
        {
            "interests": [],
            "theme_color": "default",
            "language": "en",
        },
    )

    return ProfileResponse(
        id=str(user["_id"]),
        username=user.get("username", ""),
        email=user.get("email", ""),
        full_name=user.get("full_name"),
        bio=user.get("bio"),
        avatar_url=user.get("avatar_url"),
        created_at=user.get("created_at", datetime.utcnow()),
        subscription=subscription,
        stats=stats,
        notification_preferences=notification_preferences,
        preferences=preferences,
        two_factor_enabled=user.get("two_factor_enabled", False),
    )


@router.put("/profile")
async def update_profile(
    profile_update: ProfileUpdate,
    current_user: dict = Depends(get_firebase_user),
):
    """Update user profile information"""
    user_id = current_user.get("user_id")

    update_data = {}
    if profile_update.full_name is not None:
        update_data["full_name"] = profile_update.full_name
    if profile_update.bio is not None:
        update_data["bio"] = profile_update.bio

    if not update_data:
        return {"message": "No changes requested"}

    update_data["updated_at"] = datetime.utcnow()

    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})

    # Even if modified_count is 0 (same data), we return success
    return {"message": "Profile updated successfully"}


@router.patch("/profile", response_model=ProfilePatchResponse)
async def patch_user_profile(
    body: ProfilePatchRequest,
    current_user: dict = Depends(get_firebase_user),
) -> ProfilePatchResponse:
    """Partially update user profile (display name, bio)"""
    if body.full_name is None and body.bio is None:
        raise HTTPException(
            status_code=400,
            detail="At least one field (full_name, bio) must be provided.",
        )

    user_id = current_user.get("user_id")

    update_dict: dict = {}
    if body.full_name is not None:
        update_dict["full_name"] = body.full_name
    if body.bio is not None:
        update_dict["bio"] = body.bio

    updated_at = datetime.utcnow()
    update_dict["updated_at"] = updated_at

    result = await users_collection.update_one(
        {"_id": ObjectId(user_id)}, {"$set": update_dict}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")

    return ProfilePatchResponse(
        message="Profile updated successfully",
        full_name=body.full_name,
        bio=body.bio,
        updated_at=updated_at,
    )


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_firebase_user),
):
    """Upload user avatar"""
    user_id = current_user.get("user_id")

    # Validate file type
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    # Validate file size (2MB max)
    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size must be less than 2MB")

    # Store as base64 (in production, use S3/Cloud Storage)
    base64_image = base64.b64encode(contents).decode("utf-8")
    avatar_url = f"data:{file.content_type};base64,{base64_image}"

    # Update user
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"avatar_url": avatar_url, "updated_at": datetime.utcnow()}},
    )

    return {"message": "Avatar uploaded successfully", "avatar_url": avatar_url}


@router.put("/password")
async def change_password(
    password_data: PasswordChange,
    current_user: dict = Depends(get_firebase_user),
):
    """Change user password"""
    user_id = current_user.get("user_id")

    # Get user
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Guard: Firebase-authenticated users have no local password
    if not user.get("password"):
        raise HTTPException(
            status_code=400,
            detail="Password is managed by your authentication provider. Use the client-side flow to update it.",
        )

    # Verify current password
    stored_password = user.get("password")
    if not bcrypt.checkpw(
        password_data.current_password.encode("utf-8"), stored_password.encode("utf-8")
    ):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Hash new password
    hashed_password = bcrypt.hashpw(
        password_data.new_password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # Update password
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"password": hashed_password, "updated_at": datetime.utcnow()}},
    )

    return {"message": "Password changed successfully"}


@router.put("/notifications")
async def update_notification_preferences(
    preferences: NotificationPreferences,
    current_user: dict = Depends(get_firebase_user),
):
    """Update notification preferences"""
    user_id = current_user.get("user_id")

    update_data = {}
    if preferences.email_digest is not None:
        update_data["notification_preferences.email_digest"] = preferences.email_digest
    if preferences.study_reminders is not None:
        update_data["notification_preferences.study_reminders"] = (
            preferences.study_reminders
        )
    if preferences.news_updates is not None:
        update_data["notification_preferences.news_updates"] = preferences.news_updates
    if preferences.marketing is not None:
        update_data["notification_preferences.marketing"] = preferences.marketing

    if not update_data:
        raise HTTPException(
            status_code=400,
            detail="At least one notification preference must be provided.",
        )

    update_data["updated_at"] = datetime.utcnow()

    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})

    return {"message": "Notification preferences updated successfully"}


@router.put("/preferences/general")
async def update_general_preferences(
    prefs: UserPreferences,
    current_user: dict = Depends(get_firebase_user),
):
    """Update general user preferences (Interests, Theme, Language)"""
    user_id = current_user.get("user_id")

    update_data = {}
    if prefs.interests is not None:
        update_data["preferences.interests"] = prefs.interests
    if prefs.theme_color is not None:
        update_data["preferences.theme_color"] = prefs.theme_color
    if prefs.language is not None:
        update_data["preferences.language"] = prefs.language
    if prefs.pomodoro_work_minutes is not None:
        update_data["preferences.pomodoro_work_minutes"] = prefs.pomodoro_work_minutes
    if prefs.pomodoro_short_break_minutes is not None:
        update_data["preferences.pomodoro_short_break_minutes"] = prefs.pomodoro_short_break_minutes
    if prefs.pomodoro_long_break_minutes is not None:
        update_data["preferences.pomodoro_long_break_minutes"] = prefs.pomodoro_long_break_minutes
    if prefs.pomodoro_auto_start is not None:
        update_data["preferences.pomodoro_auto_start"] = prefs.pomodoro_auto_start
    if prefs.pomodoro_enabled is not None:
        update_data["preferences.pomodoro_enabled"] = prefs.pomodoro_enabled
    if prefs.favorite_news is not None:
        # Convert Pydantic models to dicts for MongoDB
        update_data["preferences.favorite_news"] = [article.dict() for article in prefs.favorite_news]
        print(f"📰 Updating favorite_news for user {user_id}: {len(prefs.favorite_news)} articles")

    update_data["updated_at"] = datetime.utcnow()

    result = await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})
    print(f"✅ Update result: matched={result.matched_count}, modified={result.modified_count}")

    return {"message": "Preferences updated successfully"}


@router.post("/complete-wizard")
async def complete_wizard(current_user: dict = Depends(get_firebase_user)):
    """Mark the onboarding wizard as completed"""
    user_id = current_user.get("user_id")
    
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"wizard_completed": True, "updated_at": datetime.utcnow()}}
    )
    
    return {"message": "Wizard completed successfully"}


@router.post("/2fa/enable")
async def enable_2fa(current_user: dict = Depends(get_firebase_user)):
    """Enable two-factor authentication"""
    user_id = current_user.get("user_id")

    # Generate backup codes
    backup_codes = [secrets.token_hex(8) for _ in range(10)]

    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "two_factor_enabled": True,
                "two_factor_backup_codes": backup_codes,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    return {"message": "2FA enabled successfully", "backup_codes": backup_codes}


@router.post("/2fa/disable")
async def disable_2fa(current_user: dict = Depends(get_firebase_user)):
    """Disable two-factor authentication"""
    user_id = current_user.get("user_id")

    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$unset": {
                "two_factor_enabled": "",
                "two_factor_secret": "",
                "two_factor_backup_codes": "",
            },
            "$set": {"updated_at": datetime.utcnow()},
        },
    )

    return {"message": "2FA disabled successfully"}


@router.delete("/account")
async def delete_account(current_user: dict = Depends(get_firebase_user)):
    """
    Soft delete user account and all associated data.
    Data can be recovered within 30 days before permanent deletion.
    """
    from datetime import datetime, timedelta
    from app.config.database import (
        annual_plans_collection,
        focus_areas_collection,
        priorities_collection,
        goals_collection,
        activities_collection,
        daily_routines_collection,
    )
    
    user_id = current_user.get("user_id")
    now = datetime.utcnow()
    
    # 1. Soft delete user account
    user_update = await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "deleted_at": now,
                "deleted_by": user_id,
                "updated_at": now
            }
        }
    )
    
    if user_update.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    
    # 2. Cascade soft delete to all user content
    soft_delete_update = {
        "$set": {
            "deleted_at": now,
            "deleted_by": user_id,
            "is_public": False,  # Auto-unpublish
            "updated_at": now
        }
    }
    
    # Books (and their public metadata)
    await books_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Decks (and auto-unpublish)
    await decks_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Study Cards
    await study_cards_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Annual Plans
    await annual_plans_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Focus Areas
    await focus_areas_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Goals
    await goals_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Priorities
    await priorities_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Activities
    await activities_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    # Daily Routines
    await daily_routines_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    return {
        "message": "Account deleted successfully. Data can be recovered within 30 days by contacting support.",
        "recovery_deadline": (datetime.utcnow() + timedelta(days=30)).isoformat()
    }


# Legacy endpoint for backward compatibility
@router.post("/create_user", response_model=User)
async def create_user(user: User):
    """Create a new user (legacy endpoint)"""
    # Check if the user already exists
    existing_user = await users_collection.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")

    # Hash the password
    hashed_password = bcrypt.hashpw(
        user.password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    user_dict = user.dict()
    user_dict["password"] = hashed_password
    user_dict["created_at"] = datetime.utcnow()

    # Explicitly set default subscription to Free Tier
    user_dict["subscription"] = {"tier": SubscriptionTier.FREE, "status": "active"}

    # Set default role to 'user'
    user_dict["role"] = "user"
    
    # Initialize wizard status
    user_dict["wizard_completed"] = False

    # Insert the new user into the database
    insert_result = await users_collection.insert_one(user_dict)

    # Store ID properly
    user_dict["_id"] = str(insert_result.inserted_id)

    return user_dict
