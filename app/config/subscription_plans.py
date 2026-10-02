"""
Subscription Plans and Limits Configuration
Defines the features and limitations for each subscription tier.

Tiers (ADR-041, the tier contract):
  - free:  everything by hand unlimited; AI as a taste in per-call sizes under a
           monthly ceiling; 50 companion messages; one portrait at first reveal.
  - plus:  $8.99/month — book-level AI, read-aloud, companion memory and personality.
  - pro:   $19.99/month — deck-level AI, any language read-aloud, top model.
Limits are -1 for unlimited. The two "fair use" ceilings (ai_calls_per_month on
paid tiers, tts_chars_per_month) sit well above normal use and exist so a single
heavy account cannot cost more than it pays.
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
            # Study Buddy / Agent feature flags (GTM-004: only flags a gate reads live here)
            "agent_persistent_memory": False, # Session-only memory
            "agent_custom_personality": False, # Default personality only
        },
        "limits": {
            "books": -1,
            "flashcards": -1,
            "quiz_questions": -1,
            "visual_diagrams": -1,
            "import_cards": 2000,             # Anki cards a free account may bring in
            "ai_calls_per_month": 50,         # the monthly safety ceiling on AI generation calls
            "agent_messages_per_month": 50,   # 50 "Knowledge Sparks"
            "document_words": 200_000,        # one ceiling per document, every tier
            "tts_chars_per_month": 0,         # read-aloud is Plus and Pro
        },
    },
    SubscriptionTier.PLUS: {
        "name": "Plus",
        "price_cents": 899,  # $8.99
        "features": {
            "ai_content_generation": True,
            # Study Buddy / Agent feature flags
            "agent_persistent_memory": True,  # Remembers across sessions
            "agent_custom_personality": True,  # Custom vibe/name
        },
        "limits": {
            "books": -1,
            "flashcards": -1,
            "quiz_questions": -1,
            "visual_diagrams": -1,
            "import_cards": -1,
            "ai_calls_per_month": 1000,       # fair use
            "agent_messages_per_month": -1,   # Unlimited
            "document_words": 200_000,
            "tts_chars_per_month": 1_000_000, # fair use
        },
    },
    SubscriptionTier.PRO: {
        "name": "Pro",
        "price_cents": 1999,  # $19.99
        "features": {
            "ai_content_generation": True,
            # Study Buddy / Agent feature flags
            "agent_persistent_memory": True,
            "agent_custom_personality": True,
        },
        "limits": {
            "books": -1,
            "flashcards": -1,
            "quiz_questions": -1,
            "visual_diagrams": -1,
            "import_cards": -1,
            "ai_calls_per_month": 1000,       # fair use
            "agent_messages_per_month": -1,   # Unlimited
            "document_words": 200_000,
            "tts_chars_per_month": 1_000_000, # fair use
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

# Monthly ceiling on AI generation calls per tier, enforced by track_ai_usage
# (GTM-003). Derived from the plan table so there is one place to change it;
# the web reads the free value from FREE_AI_CALLS_PER_MONTH in @nowry/core.
AI_USAGE_LIMITS: dict = {
    tier: plan["limits"]["ai_calls_per_month"] for tier, plan in SUBSCRIPTION_PLANS.items()
}

# The free taste, per call (v1.0 CARD-01 / QUIZ-01 / ILLUS-01): small outputs,
# unlimited calls under the monthly ceiling above.
FREE_PER_CALL: dict = {"cards": 2, "quiz_questions": 5, "illustrations": 2}


def plan_limit(tier: str, key: str) -> int:
    """A plan's limit by key, reading an unknown tier as free. -1 means unlimited."""
    try:
        plan = SUBSCRIPTION_PLANS[SubscriptionTier(tier)]
    except (ValueError, KeyError):
        plan = SUBSCRIPTION_PLANS[SubscriptionTier.FREE]
    return int(plan["limits"].get(key, -1))
