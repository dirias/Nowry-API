"""
Phase 7 — Smart Pet tier enforcement tests.
Stubs written in Wave 0; implementations follow in Wave 2.
"""
import sys
from unittest.mock import MagicMock, AsyncMock

# Python 3.9 compatibility stubs for google-generativeai
for mod in ["google.generativeai", "google.generativeai.types",
            "google.generativeai.protos", "google.api_core.exceptions"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import pytest


# --------------------------------------------------------------------------- #
# PET-02 — Free tier: session-only memory
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_free_tier_does_not_load_persistent_history(mock_users_collection, mock_firebase_user):
    """POST /agent/chat for Free tier must NOT query MongoDB for chat_history."""
    pytest.skip("Wave 0 stub — implement in 07-03-PLAN.md")


# --------------------------------------------------------------------------- #
# PET-02 — Free tier: atomic message cap
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_free_tier_message_cap_returns_429_at_limit(mock_users_collection, mock_firebase_user):
    """POST /agent/chat returns 429 when Free tier user has used 50 messages this month."""
    pytest.skip("Wave 0 stub — implement in 07-03-PLAN.md")


@pytest.mark.asyncio
async def test_message_cap_atomic_update_uses_lt_guard(mock_users_collection, mock_firebase_user):
    """POST /agent/chat uses atomic $inc with $lt guard — matched_count=0 triggers 429."""
    pytest.skip("Wave 0 stub — implement in 07-03-PLAN.md")


# --------------------------------------------------------------------------- #
# PET-03 — Plus: personality generation limit = 1/month
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_plus_personality_limit_returns_402_on_second_generation(mock_users_collection, mock_firebase_user):
    """POST /agent/generate-personality returns 402 when Plus user has used 1 generation this month."""
    pytest.skip("Wave 0 stub — implement in 07-03-PLAN.md")


# --------------------------------------------------------------------------- #
# PET-04 — Pro: personality generation limit = 3/month
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pro_personality_limit_returns_402_on_fourth_generation(mock_users_collection, mock_firebase_user):
    """POST /agent/generate-personality returns 402 when Pro user has used 3 generations this month."""
    pytest.skip("Wave 0 stub — implement in 07-03-PLAN.md")


# --------------------------------------------------------------------------- #
# PET-02 — Free tier: personality generation blocked
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_free_tier_personality_generation_returns_403(mock_users_collection, mock_firebase_user):
    """POST /agent/generate-personality returns 403 for Free tier (feature not available)."""
    pytest.skip("Wave 0 stub — implement in 07-03-PLAN.md")
