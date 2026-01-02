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
from pydantic import BaseModel, EmailStr, validator

from app.models.User import User
from app.config.database import (
    users_collection,
    study_cards_collection,
    books_collection,
    book_pages_collection,
    decks_collection,
)
from app.auth.auth import get_current_user_authorization

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={404: {"description": "Not found"}},
)


@router.get("/me")
async def get_current_user_profile(
    current_user: dict = Depends(get_current_user_authorization),
):
    """Get current user profile"""
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Return safe user data
    return {
        "id": str(user["_id"]),
        "username": user.get("username"),
        "email": user.get("email"),
        "role": user.get("role", "user"),
        "subscription": user.get("subscription", {}),
        "preferences": user.get("preferences", {}),
        "created_at": user.get("created_at"),
        "wizard_completed": user.get("wizard_completed", False),
    }


# Pydantic Models
class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    bio: Optional[str] = None


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


class UserPreferences(BaseModel):
    interests: Optional[List[str]] = None
    theme_color: Optional[str] = None
    language: Optional[str] = None


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

        # Get total cards (Cards use ObjectId)
        total_cards = await study_cards_collection.count_documents(
            {"user_id": user_oid}
        )

        # Get flashcards only (for subscription limits)
        flashcards_count = await study_cards_collection.count_documents(
            {"user_id": user_oid, "card_type": {"$in": [None, "flashcard"]}}
        )

        # Get reviewed cards (Cards use ObjectId)
        reviewed_cards = await study_cards_collection.count_documents(
            {"user_id": user_oid, "last_reviewed": {"$exists": True}}
        )

        # Get books created (Books use String)
        books_created = await books_collection.count_documents({"user_id": user_id})

        # Calculate study streak (uses cards, so ObjectId)
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        streak = 0
        check_date = today

        while True:
            day_start = check_date
            day_end = check_date + timedelta(days=1)

            reviewed_today = (
                await study_cards_collection.count_documents(
                    {
                        "user_id": user_oid,
                        "last_reviewed": {"$gte": day_start, "$lt": day_end},
                    }
                )
                > 0
            )

            if reviewed_today:
                streak += 1
                check_date -= timedelta(days=1)
            else:
                break

        # Get quiz questions
        quiz_questions = await study_cards_collection.count_documents(
            {"user_id": user_oid, "card_type": "quiz"}
        )

        # Get visual diagrams
        visual_diagrams = await study_cards_collection.count_documents(
            {"user_id": user_oid, "card_type": "visual"}
        )

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
@router.get("/profile")
async def get_profile(current_user: dict = Depends(get_current_user_authorization)):
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

    return {
        "id": str(user["_id"]),
        "username": user.get("username", ""),
        "email": user.get("email", ""),
        "full_name": user.get("full_name"),
        "bio": user.get("bio"),
        "avatar_url": user.get("avatar_url"),
        "created_at": user.get("created_at", datetime.utcnow()),
        "subscription": subscription,
        "stats": stats,
        "notification_preferences": notification_preferences,
        "preferences": preferences,
    }


@router.put("/profile")
async def update_profile(
    profile_update: ProfileUpdate,
    current_user: dict = Depends(get_current_user_authorization),
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


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user_authorization),
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
    current_user: dict = Depends(get_current_user_authorization),
):
    """Change user password"""
    user_id = current_user.get("user_id")

    # Get user
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

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
    current_user: dict = Depends(get_current_user_authorization),
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

    update_data["updated_at"] = datetime.utcnow()

    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})

    return {"message": "Notification preferences updated successfully"}


@router.put("/preferences/general")
async def update_general_preferences(
    prefs: UserPreferences,
    current_user: dict = Depends(get_current_user_authorization),
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

    update_data["updated_at"] = datetime.utcnow()

    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})

    return {"message": "Preferences updated successfully"}


@router.post("/complete-wizard")
async def complete_wizard(current_user: dict = Depends(get_current_user_authorization)):
    """Mark the onboarding wizard as completed"""
    user_id = current_user.get("user_id")
    
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"wizard_completed": True, "updated_at": datetime.utcnow()}}
    )
    
    return {"message": "Wizard completed successfully"}


@router.post("/2fa/enable")
async def enable_2fa(current_user: dict = Depends(get_current_user_authorization)):
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
async def disable_2fa(current_user: dict = Depends(get_current_user_authorization)):
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
async def delete_account(current_user: dict = Depends(get_current_user_authorization)):
    """Delete user account and all associated data"""
    user_id = current_user.get("user_id")

    # Delete all user data
    await books_collection.delete_many({"user_id": user_id})
    await book_pages_collection.delete_many({"user_id": user_id})
    await study_cards_collection.delete_many({"user_id": user_id})
    await decks_collection.delete_many({"user_id": user_id})

    # Delete user
    result = await users_collection.delete_one({"_id": ObjectId(user_id)})

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")

    return {"message": "Account deleted successfully"}


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
