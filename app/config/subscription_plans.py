"""
Subscription Plans and Limits Configuration
Defines the features and limitations for each subscription tier.

Tiers:
  - free:  Basic access, limited resources, Study Buddy with knowledge spark cap.
  - plus:  $8.99/month — Enhanced limits, persistent pet memory, better AI model.
  - pro:   $19.99/month — Full access, vision/scan, unlimited agent messages, top model.
"""

from enum import Enum
import os


class SubscriptionTier(str, Enum):
    FREE = "free"
    PLUS = "plus"
    PRO = "pro"


# Agent model selection per tier.
# These map to Google Generative AI model identifiers.
AGENT_MODELS = {
    SubscriptionTier.FREE: "models/gemini-flash-latest",
    SubscriptionTier.PLUS: "models/gemini-flash-latest",
    SubscriptionTier.PRO: "models/gemini-pro-latest",
}


SUBSCRIPTION_PLANS = {
    SubscriptionTier.FREE: {
        "name": "Free",
        "price_cents": 0,
        "features": {
            "ai_content_generation": False,
            "export": False,
            "advanced_analytics": False,
            "custom_themes": False,
            "collaboration": False,
            # Study Buddy / Agent feature flags
            "agent_enabled": True,           # All tiers get the buddy
            "agent_vision": False,            # No image/book scanning
            "agent_persistent_memory": False, # Session-only memory
            "agent_custom_personality": False, # Default personality only
        },
        "limits": {
            "books": 3,
            "pages_per_book": 50,
            "flashcards": 50,
            "quiz_questions": 10,
            "visual_diagrams": 5,
            "ai_generations_per_month": 0,
            "agent_messages_per_month": 50,   # 50 "Knowledge Sparks"
        },
    },
    SubscriptionTier.PLUS: {
        "name": "Plus",
        "price_cents": 899,  # $8.99
        "features": {
            "ai_content_generation": True,
            "export": True,
            "advanced_analytics": False,
            "custom_themes": True,
            "collaboration": False,
            # Study Buddy / Agent feature flags
            "agent_enabled": True,
            "agent_vision": False,            # Vision is Pro-only
            "agent_persistent_memory": True,  # Remembers across sessions
            "agent_custom_personality": True,  # Custom vibe/name
        },
        "limits": {
            "books": 15,
            "pages_per_book": 200,
            "flashcards": 500,
            "quiz_questions": 100,
            "visual_diagrams": 50,
            "ai_generations_per_month": 100,
            "agent_messages_per_month": -1,   # Unlimited
        },
    },
    SubscriptionTier.PRO: {
        "name": "Pro",
        "price_cents": 1999,  # $19.99
        "features": {
            "ai_content_generation": True,
            "export": True,
            "advanced_analytics": True,
            "custom_themes": True,
            "collaboration": True,
            # Study Buddy / Agent feature flags
            "agent_enabled": True,
            "agent_vision": True,             # Full PDF/book scanning
            "agent_persistent_memory": True,
            "agent_custom_personality": True,
        },
        "limits": {
            "books": -1,  # Unlimited
            "pages_per_book": 500,
            "flashcards": -1,
            "quiz_questions": -1,
            "visual_diagrams": -1,
            "ai_generations_per_month": -1,
            "agent_messages_per_month": -1,   # Unlimited
        },
    },
}


# Stripe Price IDs — per D-03, never hardcode IDs in source
STRIPE_PRICE_IDS = {
    "plus_monthly": os.getenv("STRIPE_PLUS_MONTHLY_PRICE_ID"),
    "plus_annual":  os.getenv("STRIPE_PLUS_ANNUAL_PRICE_ID"),
    "pro_monthly":  os.getenv("STRIPE_PRO_MONTHLY_PRICE_ID"),
    "pro_annual":   os.getenv("STRIPE_PRO_ANNUAL_PRICE_ID"),
}

# Monthly AI generation limits per tier (used by track_ai_usage in Phase 4 enforcement)
# -1 means unlimited; 0 means Free tier has no AI card/quiz/visualizer generation
AI_USAGE_LIMITS: dict = {
    SubscriptionTier.FREE: 0,
    SubscriptionTier.PLUS: 100,
    SubscriptionTier.PRO: -1,
}
