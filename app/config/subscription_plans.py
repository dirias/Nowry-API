"""
Subscription Plans and Limits Configuration
Defines the features and limitations for each subscription tier.
"""

from enum import Enum


class SubscriptionTier(str, Enum):
    FREE = "free"
    STUDENT = "student"
    PRO = "pro"
    ENTERPRISE = "enterprise"


SUBSCRIPTION_PLANS = {
    SubscriptionTier.FREE: {
        "name": "Free",
        "price": 0,
        "features": {
            "ai_content_generation": False,
            "export": False,
            "advanced_analytics": False,
            "custom_themes": False,
            "collaboration": False,
        },
        "limits": {
            "books": 3,
            "pages_per_book": 50,
            "flashcards": 50,
            "quiz_questions": 10,
            "visual_diagrams": 5,
            "ai_generations_per_month": 0,
        },
    },
    SubscriptionTier.STUDENT: {
        "name": "Student",
        "price": 499,  # in cents
        "features": {
            "ai_content_generation": True,
            "export": True,
            "advanced_analytics": False,
            "custom_themes": True,
            "collaboration": False,
        },
        "limits": {
            "books": 15,
            "pages_per_book": 200,
            "flashcards": 500,
            "quiz_questions": 100,
            "visual_diagrams": 50,
            "ai_generations_per_month": 50,
        },
    },
    SubscriptionTier.PRO: {
        "name": "Pro",
        "price": 999,  # in cents
        "features": {
            "ai_content_generation": True,
            "export": True,
            "advanced_analytics": True,
            "custom_themes": True,
            "collaboration": True,
        },
        "limits": {
            "books": -1,  # Unlimited
            "pages_per_book": 500,
            "flashcards": -1,
            "quiz_questions": -1,
            "visual_diagrams": -1,
            "ai_generations_per_month": -1,
        },
    },
    SubscriptionTier.ENTERPRISE: {
        "name": "Enterprise",
        "price": "custom",
        "features": {
            "ai_content_generation": True,
            "export": True,
            "advanced_analytics": True,
            "custom_themes": True,
            "collaboration": True,
        },
        "limits": {
            "books": -1,
            "pages_per_book": 1000,
            "flashcards": -1,
            "quiz_questions": -1,
            "visual_diagrams": -1,
            "ai_generations_per_month": -1,
        },
    },
}
