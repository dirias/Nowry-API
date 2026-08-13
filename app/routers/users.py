"""
User Profile Router
Handles user profile management, settings, and preferences
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pymongo.collection import Collection
from bson import ObjectId
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, List
import bcrypt
import base64
import secrets
import asyncio
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, validator
from app.utils.logger import get_logger

logger = get_logger(__name__)

from app.models.User import User
from app.models.common import (
    MessageResponse,
    AvatarUploadResponse,
    TwoFactorEnableResponse,
    AccountDeleteResponse,
    DataExportResponse,
)
from app.config.database import (
    users_collection,
    study_cards_collection,
    study_sessions_collection,
    books_collection,
    decks_collection,
    tasks_collection,
    annual_plans_collection,
    focus_areas_collection,
    goals_collection,
    blackboards_collection,
)
from app.auth.firebase_auth import get_firebase_user

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)


class UserMeResponse(BaseModel):
    id: str
    firebase_uid: str
    username: str
    email: str
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    photo_url: Optional[str] = None
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
        photo_url=user.get("photo_url"),
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
    photo_url: Optional[str] = None
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


class GeneralPreferencesUpdate(BaseModel):
    """
    All fields are optional — only fields present in the request payload are written.
    Pydantic v2's model_fields_set is used in the handler to build a partial $set,
    so sending { language: 'es' } never touches theme_color, interests, etc.
    """
    model_config = ConfigDict(extra='ignore')  # Silently discard unknown legacy fields

    language: str | None = Field(
        default=None,
        pattern='^(en|es|fr|de|ja)$'
    )
    theme_color: str | None = Field(
        default=None,
        pattern='^#[0-9a-fA-F]{6}$'
    )
    primary_topic: str | None = Field(
        default=None,
        description="Single primary learning topic (the single study focus)"
    )
    interests: list[str] | None = Field(
        default=None,
        description="Multi-select interest topics used to personalize the home news feed"
    )
    study_goal: str | None = Field(
        default=None,
        pattern='^(general|academic|career|language|hobby)$'
    )
    favorite_news: list[dict] | None = Field(
        default=None,
        description="User's saved/favorited news articles"
    )
    # Study Buddy / Agent settings
    agent_knowledge_access: bool | None = Field(
        default=None,
        description="Allow the Study Buddy to read the user's library, decks, and goals"
    )
    agent_proactive_nudging: bool | None = Field(
        default=None,
        description="Allow the Study Buddy to proactively remind the user of due cards on dashboard load"
    )
    agent_conciseness: str | None = Field(
        default=None,
        pattern='^(concise|balanced|detailed)$',
        description="Controls reply length: concise (1-2 sentences), balanced (default), detailed (thorough explanations)"
    )
    agent_tone: str | None = Field(
        default=None,
        pattern='^(friendly|professional|strict|socratic)$',
        description="Controls the pet's personality style"
    )
    agent_roaming_enabled: bool | None = Field(
        default=None,
        description="Allow the Study Buddy to navigate autonomously between app sections"
    )
    agent_intervention_frequency: Optional[Literal['conservative', 'balanced', 'frequent']] = Field(
        default=None,
        description="Per-session intervention cap tier"
    )
    agent_focus_mode: Optional[bool] = Field(
        default=None,
        description="When True, wrong_answer interventions are silenced during study sessions"
    )
    agent_intervention_wrong_answer: Optional[bool] = Field(default=None)
    agent_intervention_session_summary: Optional[bool] = Field(default=None)
    agent_intervention_pre_session: Optional[bool] = Field(default=None)
    agent_intervention_re_engagement: Optional[bool] = Field(default=None)
    agent_intervention_streak_milestone: Optional[bool] = Field(default=None)
    # AI Quiz question count — Plus/Pro only; free tier is always capped at 10.
    agent_ai_quiz_question_count: int | None = Field(
        default=None,
        ge=5,
        le=20,
        description="Number of questions per AI quiz session (5–20). Ignored for free-tier users.",
    )


class GeneralPreferencesResponse(BaseModel):
    language: str
    theme_color: str
    primary_topic: str | None = None
    interests: list[str] = Field(default_factory=list)
    study_goal: str | None = None
    favorite_news: list[dict] = Field(default_factory=list)
    agent_knowledge_access: bool = False
    agent_proactive_nudging: bool = False
    agent_conciseness: str = 'balanced'
    agent_tone: str = 'friendly'
    agent_roaming_enabled: bool = True
    agent_intervention_frequency: str = 'balanced'
    agent_focus_mode: bool = False
    agent_intervention_wrong_answer: bool = True
    agent_intervention_session_summary: bool = True
    agent_intervention_pre_session: bool = True
    agent_intervention_re_engagement: bool = True
    agent_intervention_streak_milestone: bool = True
    # AI Quiz question count — always returned; effective value depends on tier (free=10 fixed).
    agent_ai_quiz_question_count: int = 10
    updated_at: datetime




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

        # Calculate study streak efficiently via a user-scoped aggregation that
        # returns already-distinct reviewed dates (avoids streaming one
        # document per reviewed card over an async-for cursor iteration).
        one_year_ago = datetime.now(timezone.utc) - timedelta(days=365)
        streak_pipeline = [
            {"$match": {"user_id": user_oid, "last_reviewed": {"$gt": one_year_ago}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$last_reviewed"}}}},
        ]
        distinct_dates = await study_cards_collection.aggregate(streak_pipeline).to_list(length=366)

        # Collect unique dates locally
        reviewed_dates = {datetime.strptime(d["_id"], "%Y-%m-%d").date() for d in distinct_dates}

        # Calculate streak
        today = datetime.now(timezone.utc).date()
        streak = 0
        check_date = today
        
        while check_date in reviewed_dates:
            streak += 1
            check_date -= timedelta(days=1)

        # Fetch ai_usage_count from user's subscription subdoc
        user_doc = await users_collection.find_one(
            {"_id": ObjectId(user_oid)},
            {"subscription.ai_usage_count": 1},
        )

        return {
            "total_cards": total_cards,
            "flashcards_count": flashcards_count,
            "reviewed_cards": reviewed_cards,
            "books_created": books_created,
            "study_streak": streak,
            "quiz_questions": quiz_questions,
            "visual_diagrams": visual_diagrams,
            "ai_generations_month": (user_doc or {}).get("subscription", {}).get("ai_usage_count", 0),
        }
    except Exception as e:
        logger.error(f"Error calculating user stats: {e}", exc_info=True)
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
        # Billing fields — convert datetime → ISO string for JSON serialization
        "ai_usage_count": stored_sub.get("ai_usage_count", 0),
        "ai_usage_reset_date": (
            stored_sub["ai_usage_reset_date"].isoformat()
            if stored_sub.get("ai_usage_reset_date") else None
        ),
        "next_billing_date": (
            stored_sub["next_billing_date"].isoformat()
            if stored_sub.get("next_billing_date") else None
        ),
        "stripe_subscription_id": stored_sub.get("stripe_subscription_id"),
        "billing_interval": stored_sub.get("billing_interval"),
        "subscription_status_updated_at": (
            stored_sub["subscription_status_updated_at"].isoformat()
            if stored_sub.get("subscription_status_updated_at") else None
        ),
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
        photo_url=user.get("photo_url"),
        created_at=user.get("created_at", datetime.now(timezone.utc)),
        subscription=subscription,
        stats=stats,
        notification_preferences=notification_preferences,
        preferences=preferences,
        two_factor_enabled=user.get("two_factor_enabled", False),
    )


@router.put("/profile", response_model=MessageResponse)
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

    update_data["updated_at"] = datetime.now(timezone.utc)

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

    updated_at = datetime.now(timezone.utc)
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


@router.post("/avatar", response_model=AvatarUploadResponse)
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
        {"$set": {"avatar_url": avatar_url, "updated_at": datetime.now(timezone.utc)}},
    )

    return {"message": "Avatar uploaded successfully", "avatar_url": avatar_url}


@router.put("/password", response_model=MessageResponse)
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
        {"$set": {"password": hashed_password, "updated_at": datetime.now(timezone.utc)}},
    )

    return {"message": "Password changed successfully"}


@router.put("/notifications", response_model=MessageResponse)
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

    update_data["updated_at"] = datetime.now(timezone.utc)

    await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})

    return {"message": "Notification preferences updated successfully"}


@router.get("/preferences/general", response_model=GeneralPreferencesResponse)
async def get_general_preferences(
    current_user: dict = Depends(get_firebase_user),
) -> GeneralPreferencesResponse:
    """Fetch the current user's general and agent preferences."""
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    general_prefs: dict = user.get("preferences", {}).get("general", {})
    agent_prefs: dict = user.get("preferences", {}).get("agent", {})
    updated_at = general_prefs.get("updated_at", datetime.now(timezone.utc))

    return GeneralPreferencesResponse(
        language=general_prefs.get("language", "en"),
        theme_color=general_prefs.get("theme_color", "#2a6971"),
        primary_topic=general_prefs.get("primary_topic"),
        interests=general_prefs.get("interests", []),
        study_goal=general_prefs.get("study_goal"),
        favorite_news=general_prefs.get("favorite_news", []),
        agent_knowledge_access=agent_prefs.get("knowledge_access", False),
        agent_proactive_nudging=agent_prefs.get("proactive_nudging", False),
        agent_conciseness=agent_prefs.get("conciseness", "balanced"),
        agent_tone=agent_prefs.get("tone", "friendly"),
        agent_roaming_enabled=bool(agent_prefs.get("roaming_enabled", True)),
        agent_intervention_frequency=agent_prefs.get("intervention_frequency", "balanced"),
        agent_focus_mode=bool(agent_prefs.get("focus_mode", False)),
        agent_intervention_wrong_answer=bool(agent_prefs.get("intervention_wrong_answer", True)),
        agent_intervention_session_summary=bool(agent_prefs.get("intervention_session_summary", True)),
        agent_intervention_pre_session=bool(agent_prefs.get("intervention_pre_session", True)),
        agent_intervention_re_engagement=bool(agent_prefs.get("intervention_re_engagement", True)),
        agent_intervention_streak_milestone=bool(agent_prefs.get("intervention_streak_milestone", True)),
        agent_ai_quiz_question_count=int(agent_prefs.get("ai_quiz_question_count", 10)),
        updated_at=updated_at,
    )


@router.put("/preferences/general", response_model=GeneralPreferencesResponse)

async def update_general_preferences(
    data: GeneralPreferencesUpdate,
    current_user: dict = Depends(get_firebase_user),
) -> GeneralPreferencesResponse:
    """Update general user preferences from the onboarding wizard."""
    user_id = current_user.get("user_id")
    updated_at = datetime.now(timezone.utc)

    # Only write fields that were explicitly included in the request payload.
    # model_fields_set contains the names of fields the client actually sent.
    field_map: dict = {
        "language":                 "preferences.general.language",
        "theme_color":              "preferences.general.theme_color",
        "primary_topic":            "preferences.general.primary_topic",
        "interests":                "preferences.general.interests",
        "study_goal":               "preferences.general.study_goal",
        "favorite_news":            "preferences.general.favorite_news",
        # Agent settings are stored under a separate 'agent' sub-key for clean separation
        "agent_knowledge_access":              "preferences.agent.knowledge_access",
        "agent_proactive_nudging":             "preferences.agent.proactive_nudging",
        "agent_conciseness":                   "preferences.agent.conciseness",
        "agent_tone":                          "preferences.agent.tone",
        "agent_roaming_enabled":               "preferences.agent.roaming_enabled",
        "agent_intervention_frequency":        "preferences.agent.intervention_frequency",
        "agent_focus_mode":                    "preferences.agent.focus_mode",
        "agent_intervention_wrong_answer":     "preferences.agent.intervention_wrong_answer",
        "agent_intervention_session_summary":  "preferences.agent.intervention_session_summary",
        "agent_intervention_pre_session":      "preferences.agent.intervention_pre_session",
        "agent_intervention_re_engagement":    "preferences.agent.intervention_re_engagement",
        "agent_intervention_streak_milestone": "preferences.agent.intervention_streak_milestone",
        "agent_ai_quiz_question_count":        "preferences.agent.ai_quiz_question_count",
    }
    set_doc: dict = {"preferences.general.updated_at": updated_at}
    for field_name, db_path in field_map.items():
        if field_name in data.model_fields_set:
            set_doc[db_path] = getattr(data, field_name)

    result = await users_collection.find_one_and_update(
        {"_id": ObjectId(user_id)},
        {"$set": set_doc},
        return_document=True,
    )

    if result is None:
        raise HTTPException(status_code=404, detail="User not found")

    general_prefs: dict = result.get("preferences", {}).get("general", {})
    agent_prefs: dict = result.get("preferences", {}).get("agent", {})

    return GeneralPreferencesResponse(
        language=general_prefs.get("language", "en"),
        theme_color=general_prefs.get("theme_color", "#2a6971"),
        primary_topic=general_prefs.get("primary_topic"),
        interests=general_prefs.get("interests", []),
        study_goal=general_prefs.get("study_goal"),
        favorite_news=general_prefs.get("favorite_news", []),
        agent_knowledge_access=agent_prefs.get("knowledge_access", False),
        agent_proactive_nudging=agent_prefs.get("proactive_nudging", False),
        agent_conciseness=agent_prefs.get("conciseness", "balanced"),
        agent_tone=agent_prefs.get("tone", "friendly"),
        agent_roaming_enabled=bool(agent_prefs.get("roaming_enabled", True)),
        agent_intervention_frequency=agent_prefs.get("intervention_frequency", "balanced"),
        agent_focus_mode=bool(agent_prefs.get("focus_mode", False)),
        agent_intervention_wrong_answer=bool(agent_prefs.get("intervention_wrong_answer", True)),
        agent_intervention_session_summary=bool(agent_prefs.get("intervention_session_summary", True)),
        agent_intervention_pre_session=bool(agent_prefs.get("intervention_pre_session", True)),
        agent_intervention_re_engagement=bool(agent_prefs.get("intervention_re_engagement", True)),
        agent_intervention_streak_milestone=bool(agent_prefs.get("intervention_streak_milestone", True)),
        agent_ai_quiz_question_count=int(agent_prefs.get("ai_quiz_question_count", 10)),
        updated_at=general_prefs.get("updated_at", updated_at),
    )


@router.post("/complete-wizard", response_model=MessageResponse)
async def complete_wizard(current_user: dict = Depends(get_firebase_user)):
    """Mark the onboarding wizard as completed"""
    user_id = current_user.get("user_id")
    
    await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"wizard_completed": True, "updated_at": datetime.now(timezone.utc)}}
    )
    
    return {"message": "Wizard completed successfully"}


@router.post("/2fa/enable", response_model=TwoFactorEnableResponse)
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
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    return {"message": "2FA enabled successfully", "backup_codes": backup_codes}


@router.post("/2fa/disable", response_model=MessageResponse)
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
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    return {"message": "2FA disabled successfully"}


@router.delete("/account", response_model=AccountDeleteResponse)
async def delete_account(current_user: dict = Depends(get_firebase_user)):
    """
    Soft delete user account and all associated data.
    Data can be recovered within 30 days before permanent deletion.

    Order per D-08: MongoDB soft-deletes are committed FIRST, then Firebase
    credentials are revoked. Firebase deletion failure is caught and logged
    (MongoDB is source of truth).
    """
    from app.config.database import (
        priorities_collection,
        activities_collection,
        daily_routines_collection,
    )

    user_id = current_user.get("user_id")
    firebase_uid: str = current_user.get("firebase_uid", "")
    now = datetime.now(timezone.utc)

    # 0. Guard: block deletion if user has public decks or books.
    #    The Browse/Fork feature (Phase 6) will create community dependencies on
    #    public content — deleting without unpublishing first would break those.
    #    Users must unpublish manually before deleting their account.
    public_deck_count = await decks_collection.count_documents(
        {"user_id": user_id, "is_public": True, "deleted_at": None}
    )
    public_book_count = await books_collection.count_documents(
        {"user_id": user_id, "is_public": True, "deleted_at": None}
    )
    if public_deck_count > 0 or public_book_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"You have {public_deck_count} public deck(s) and {public_book_count} public book(s). "
                "Please unpublish all public content before deleting your account."
            ),
        )

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

    # Study Sessions (performance history)
    await study_sessions_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        {"$set": {"deleted_at": now, "deleted_by": user_id, "updated_at": now}}
    )

    # Annual Plans
    plans_cursor = annual_plans_collection.find({"user_id": user_id, "deleted_at": None})
    plans = await plans_cursor.to_list(length=100)
    plan_ids = [str(p["_id"]) for p in plans]
    
    await annual_plans_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )
    
    if plan_ids:
        # Focus Areas
        areas_cursor = focus_areas_collection.find({"annual_plan_id": {"$in": plan_ids}, "deleted_at": None})
        areas = await areas_cursor.to_list(length=300)
        area_ids = [str(a["_id"]) for a in areas]
        
        await focus_areas_collection.update_many(
            {"annual_plan_id": {"$in": plan_ids}, "deleted_at": None},
            soft_delete_update
        )
        
        if area_ids:
            # Goals
            goals_cursor = goals_collection.find({"focus_area_id": {"$in": area_ids}, "deleted_at": None})
            goals = await goals_cursor.to_list(length=1000)
            goal_ids = [str(g["_id"]) for g in goals]
            
            await goals_collection.update_many(
                {"focus_area_id": {"$in": area_ids}, "deleted_at": None},
                soft_delete_update
            )
            
            # Priorities
            await priorities_collection.update_many(
                {"focus_area_id": {"$in": area_ids}, "deleted_at": None},
                soft_delete_update
            )
            
            if goal_ids:
                # Activities
                await activities_collection.update_many(
                    {"goal_id": {"$in": goal_ids}, "deleted_at": None},
                    soft_delete_update
                )
    
    # Tasks
    await tasks_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )

    # Daily Routines
    await daily_routines_collection.update_many(
        {"user_id": user_id, "deleted_at": None},
        soft_delete_update
    )

    # Blackboards — owned boards are soft-deleted; the deleted user is also
    # pulled from every OTHER board's collaborators array so no stale reference
    # lingers on someone else's shared board. Blackboard has no is_public field,
    # so soft_delete_update is deliberately not reused here.
    await blackboards_collection.update_many(
        {"owner_user_id": user_id, "deleted_at": None},
        {"$set": {"deleted_at": now, "deleted_by": user_id, "updated_at": now}}
    )

    await blackboards_collection.update_many(
        {"collaborators": user_id},
        {"$pull": {"collaborators": user_id}, "$set": {"updated_at": now}}
    )

    # 3. Revoke Firebase credentials AFTER all MongoDB soft-deletes (D-08)
    if firebase_uid:
        try:
            from firebase_admin import auth as firebase_auth_sdk
            # Revoke all refresh tokens FIRST — this immediately invalidates all
            # active sessions (including Google OAuth sessions) so existing ID tokens
            # fail on the next backend request. Without this, valid tokens remain
            # usable for up to 1 hour after deletion.
            firebase_auth_sdk.revoke_refresh_tokens(firebase_uid)
            firebase_auth_sdk.delete_user(firebase_uid)
        except Exception as exc:
            # MongoDB is source of truth — log but do not surface Firebase errors.
            # Account is already soft-deleted; user cannot authenticate again once
            # the token cache expires.
            logger.error(
                "Failed to revoke/delete Firebase user %s after MongoDB soft-delete: %s",
                firebase_uid,
                exc,
            )

    return AccountDeleteResponse(
        message="Account deleted successfully. Your data will be retained for 30 days before permanent removal.",
        recovery_deadline=(now + timedelta(days=30)).isoformat(),
    )


@router.post("/create_user")
async def create_user():
    """Create a new user (legacy endpoint)"""
    raise HTTPException(status_code=410, detail="This endpoint is completely deprecated. Account creation is securely handled by Firebase.")


# ---------------------------------------------------------------------------
# Pet preferences
# ---------------------------------------------------------------------------

VALID_SPECIES: frozenset[str] = frozenset({
    "owl", "fox", "cat", "dragon", "robot",
    "star", "phoenix", "crystal", "leaf", "music",
})

VALID_COLORS: frozenset[str] = frozenset({
    "ocean", "violet", "mint", "gold", "rose", "coral", "sky", "ember",
})


class PetPreferencesUpdate(BaseModel):
    pet_name: str | None = None
    pet_species: str | None = None
    pet_color: str | None = None

    @field_validator("pet_name")
    @classmethod
    def validate_pet_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) == 0:
            return None
        if len(v) > 20:
            raise ValueError("pet_name must be 20 characters or fewer")
        return v

    @field_validator("pet_species")
    @classmethod
    def validate_pet_species(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_SPECIES:
            raise ValueError(f"pet_species must be one of {sorted(VALID_SPECIES)}")
        return v

    @field_validator("pet_color")
    @classmethod
    def validate_pet_color(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_COLORS:
            raise ValueError(f"pet_color must be one of {sorted(VALID_COLORS)}")
        return v


class PetPreferencesResponse(BaseModel):
    pet_name: str | None = None
    pet_species: str | None = None
    pet_color: str | None = None


@router.get("/preferences/pet", response_model=PetPreferencesResponse)
async def get_pet_preferences(
    current_user: dict = Depends(get_firebase_user),
) -> PetPreferencesResponse:
    """Fetch the current user's pet customization preferences."""
    user_id: str = current_user.get("user_id")
    user_doc = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"preferences.pet": 1},
    )
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    pet: dict = user_doc.get("preferences", {}).get("pet", {})
    return PetPreferencesResponse(
        pet_name=pet.get("pet_name"),
        pet_species=pet.get("pet_species"),
        pet_color=pet.get("pet_color"),
    )


@router.put("/preferences/pet", response_model=PetPreferencesResponse)
async def update_pet_preferences(
    body: PetPreferencesUpdate,
    current_user: dict = Depends(get_firebase_user),
) -> PetPreferencesResponse:
    """
    Partially update the current user's pet preferences.

    Only fields explicitly included in the request body are written to the
    database — omitting a field never nulls an existing stored value.
    """
    user_id: str = current_user.get("user_id")

    update_fields: dict[str, object] = {}
    for field in body.model_fields_set:
        update_fields[f"preferences.pet.{field}"] = getattr(body, field)

    if not update_fields:
        return await get_pet_preferences(current_user=current_user)

    result = await users_collection.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": update_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")

    return await get_pet_preferences(current_user=current_user)


# ---------------------------------------------------------------------------
# Data export helper
# ---------------------------------------------------------------------------

def _serialize_doc(doc: dict) -> dict:
    """Convert a MongoDB document to a JSON-serializable dict.

    Handles ObjectId → str, datetime → ISO string, and nested structures.
    """
    result = {}
    for key, value in doc.items():
        if type(value).__name__ == "ObjectId":
            result[key] = str(value)
        elif hasattr(value, "isoformat"):  # datetime / date
            result[key] = value.isoformat()
        elif isinstance(value, dict):
            result[key] = _serialize_doc(value)
        elif isinstance(value, list):
            result[key] = [
                _serialize_doc(v) if isinstance(v, dict)
                else str(v) if type(v).__name__ == "ObjectId"
                else v
                for v in value
            ]
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# GDPR export endpoint
# ---------------------------------------------------------------------------

@router.get("/export", summary="Export all user data as JSON download")
async def export_user_data(
    current_user: dict = Depends(get_firebase_user),
) -> None:
    """
    Export all user content as a downloadable JSON file.

    Includes: books, decks, cards, tasks, annual plans, goals.
    Does NOT include session history or quiz records (out of scope per D-13).
    All queries filter deleted_at=None (export only active content).
    Queries run in parallel via asyncio.gather for fast response times.
    """
    user_id: str = current_user.get("user_id")
    user_email: str = current_user.get("email", "")

    # Parallel fetch from 5 directly user_id-indexed collections
    books_coro = books_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=10000)

    decks_coro = decks_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=10000)

    cards_coro = study_cards_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=50000)

    tasks_coro = tasks_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=10000)

    plans_coro = annual_plans_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=100)

    books, decks, cards, tasks, plans = await asyncio.gather(
        books_coro, decks_coro, cards_coro, tasks_coro, plans_coro
    )

    # Goals are linked via focus_areas (not directly by user_id)
    # Fetch focus areas for this user's plans, then goals for those focus areas
    goals: list = []
    if plans:
        plan_ids = [str(p["_id"]) for p in plans]
        focus_areas = await focus_areas_collection.find(
            {"annual_plan_id": {"$in": plan_ids}, "deleted_at": None}
        ).to_list(length=1000)
        if focus_areas:
            focus_area_ids = [str(fa["_id"]) for fa in focus_areas]
            goals = await goals_collection.find(
                {"focus_area_id": {"$in": focus_area_ids}, "deleted_at": None}
            ).to_list(length=5000)

    now = datetime.now(timezone.utc)
    filename = f"nowry_export_{now.strftime('%Y%m%d_%H%M%S')}.json"

    export_data = {
        "exported_at": now.isoformat(),
        "user_email": user_email,
        "books": [_serialize_doc(d) for d in books],
        "decks": [_serialize_doc(d) for d in decks],
        "cards": [_serialize_doc(d) for d in cards],
        "tasks": [_serialize_doc(d) for d in tasks],
        "annual_plans": [_serialize_doc(d) for d in plans],
        "goals": [_serialize_doc(d) for d in goals],
    }

    return JSONResponse(
        content=export_data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
