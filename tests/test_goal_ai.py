"""
Phase 8 — Goal AI tests. Stubs written in Wave 0; implementations follow in Wave 1.
All tests are skipped until routers/goal_ai.py exists.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, AsyncMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Module-level sys.modules stubs — installed before any app import
# (mirrors the approach used in test_ai_magic.py and test_stripe_webhooks.py)
# ─────────────────────────────────────────────────────────────────────────────

if "groq" not in sys.modules:
    sys.modules["groq"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()
if "google.generativeai" not in sys.modules:
    sys.modules["google.generativeai"] = MagicMock()

mock_firebase = MagicMock()
mock_firebase.get_firebase_user = AsyncMock(return_value={"user_id": "507f1f77bcf86cd799439011"})
sys.modules.setdefault("app.auth.firebase_auth", mock_firebase)

mock_deps = MagicMock()
mock_deps.get_subscription_tier = AsyncMock(return_value="free")
mock_deps.track_ai_usage = AsyncMock(return_value={"user_id": "507f1f77bcf86cd799439011"})
sys.modules.setdefault("app.auth.dependencies", mock_deps)

if "app.config.database" not in sys.modules:
    sys.modules["app.config.database"] = MagicMock()
if "app.ai_orchestrator.llm_clients.gemini_client" not in sys.modules:
    sys.modules["app.ai_orchestrator.llm_clients.gemini_client"] = MagicMock()


# ─────────────────────────────────────────────────────────────────────────────
# GOAL-01: POST /goal-ai/analyze returns suggestions
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-03-PLAN")
@pytest.mark.asyncio
async def test_analyze_goals_pro_returns_suggestions(mock_user_doc_pro):
    """GOAL-01: Pro tier — POST /goal-ai/analyze response has `suggestions` field (list)."""
    from app.routers.goal_ai import analyze_goals

    result = await analyze_goals(
        current_user=mock_user_doc_pro,
        tier="pro",
    )

    assert hasattr(result, "suggestions")
    assert isinstance(result.suggestions, list)


# ─────────────────────────────────────────────────────────────────────────────
# GOAL-02: POST /goal-ai/analyze returns conflicts
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-03-PLAN")
@pytest.mark.asyncio
async def test_analyze_goals_returns_conflicts(mock_user_doc_pro):
    """GOAL-02: Pro tier — POST /goal-ai/analyze response has `conflicts` field (list)."""
    from app.routers.goal_ai import analyze_goals

    result = await analyze_goals(
        current_user=mock_user_doc_pro,
        tier="pro",
    )

    assert hasattr(result, "conflicts")
    assert isinstance(result.conflicts, list)


# ─────────────────────────────────────────────────────────────────────────────
# GOAL-03: POST /goal-ai/analyze returns archiving_recommendations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-03-PLAN")
@pytest.mark.asyncio
async def test_analyze_goals_returns_archiving(mock_user_doc_pro):
    """GOAL-03: Pro tier — POST /goal-ai/analyze response has `archiving_recommendations` field (list)."""
    from app.routers.goal_ai import analyze_goals

    result = await analyze_goals(
        current_user=mock_user_doc_pro,
        tier="pro",
    )

    assert hasattr(result, "archiving_recommendations")
    assert isinstance(result.archiving_recommendations, list)


# ─────────────────────────────────────────────────────────────────────────────
# D-06: Free/Plus tier gate — POST /goal-ai/analyze returns 403
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-03-PLAN")
@pytest.mark.asyncio
async def test_analyze_goals_free_tier_403(mock_user_doc):
    """D-06: Free tier calling POST /goal-ai/analyze raises HTTPException(403)."""
    from fastapi import HTTPException
    from app.routers.goal_ai import analyze_goals

    user_doc = dict(mock_user_doc)
    user_doc["user_id"] = "507f1f77bcf86cd799439011"

    with pytest.raises(HTTPException) as exc_info:
        await analyze_goals(
            current_user=user_doc,
            tier="free",
        )

    assert exc_info.value.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Edge case: No annual plan in DB → 404
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skip(reason="Implementing in 08-03-PLAN")
@pytest.mark.asyncio
async def test_analyze_goals_no_annual_plan_404(mock_user_doc_pro):
    """POST /goal-ai/analyze raises HTTPException(404) when no annual plan exists in DB."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.routers.goal_ai import analyze_goals

    with patch("app.routers.goal_ai.annual_plans_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc_info:
            await analyze_goals(
                current_user=mock_user_doc_pro,
                tier="pro",
            )

    assert exc_info.value.status_code == 404
