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

# Phase 31: app.routers.agent also transitively imports slowapi (app.core.limiter),
# langfuse, and app.auth.firebase_auth (which uses Python 3.10+ `dict | None` syntax
# that fails to import on this Python 3.9 test runner) — none of these are exercised
# by the Phase 31 tests below, so they are stubbed the same way test_ai_magic.py /
# test_tracing.py stub them for other routers (slowapi/limiter not installed locally;
# firebase_auth needs a real-import bypass, not a functional mock).
for mod in ["slowapi", "slowapi.util", "langfuse"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

if "app.core.limiter" not in sys.modules:
    _mock_limiter_mod = MagicMock()
    _mock_limiter_instance = MagicMock()
    _mock_limiter_instance.limit = lambda *a, **k: (lambda fn: fn)
    _mock_limiter_mod.limiter = _mock_limiter_instance
    sys.modules["app.core.limiter"] = _mock_limiter_mod

if "app.auth.firebase_auth" not in sys.modules:
    _mock_firebase_auth_mod = MagicMock()
    _mock_firebase_auth_mod.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock_firebase_auth_mod

# Full-suite ordering guard (mirrors test_tracing.py's _ensure_agent_importable()):
# when the whole tests/ directory is collected together, test_ai_magic.py's
# _ensure_cards_importable() (which alphabetically collects first) registers
# app.models.quiz as a bare MagicMock. A MagicMock's .QuizConfig attribute is
# itself a MagicMock, which breaks Pydantic's `QuizConfig | None` schema
# generation in agent.py's ChatResponse. Unconditionally overwrite .QuizConfig/
# .QuizOffer with real minimal Pydantic models so agent.py imports cleanly
# regardless of collection order.
_quiz_mod = sys.modules.get("app.models.quiz")
if _quiz_mod is None or not hasattr(_quiz_mod, "QuizConfig") or not isinstance(
    getattr(_quiz_mod, "QuizConfig", None), type
):
    from typing import Optional as _Optional
    from pydantic import BaseModel as _BM

    class _QuizConfig(_BM):
        mode: str
        topic: _Optional[str] = None
        question_count: int = 10
        deck_id: _Optional[str] = None

    class _QuizOffer(_BM):
        topic: str
        mode: str = "ai"
        question_count: int = 10

    if _quiz_mod is None:
        _quiz_mod = MagicMock()
        sys.modules["app.models.quiz"] = _quiz_mod
    _quiz_mod.QuizConfig = _QuizConfig
    _quiz_mod.QuizOffer = _QuizOffer

# Same full-suite ordering guard for app.config.subscription_plans and
# app.models.agent_models — other test files earlier in collection order
# (alphabetically) may have registered these as bare MagicMock()s, which
# breaks agent.py's real subscription-tier gating logic and FastAPI's
# response_model=GeneratePersonalityResponse schema generation respectively.
# Unconditionally re-import the real modules (mirrors test_tracing.py's
# _ensure_agent_importable(); neither module has problematic transitive
# imports, so this is safe regardless of what ran before this file).
import importlib as _importlib

sys.modules.pop("app.config.subscription_plans", None)
sys.modules["app.config.subscription_plans"] = _importlib.import_module(
    "app.config.subscription_plans"
)

sys.modules.pop("app.models.agent_models", None)
sys.modules["app.models.agent_models"] = _importlib.import_module(
    "app.models.agent_models"
)

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


# --------------------------------------------------------------------------- #
# Phase 31 — MODE-06/MODE-07: mode parsing, mode-aware context injection,
# and session_summary wording (LLM template + rule-based fallback).
# RED tests — these fail on assertion until 31-04-PLAN.md implements the
# mode field, the mode-aware injection branch, and the reworded strings.
# --------------------------------------------------------------------------- #
from app.routers.agent import (  # noqa: E402
    ScreenContextPayload,
    _build_context_injection,
    _build_session_summary_message,
    _INTERVENTION_PROMPT_TEMPLATES,
    InterventionRequest,
)


# --- MODE-06: ScreenContextPayload.mode parsing (Pitfall 2) --- #
def test_screen_context_payload_parses_mode():
    """A posted `mode` field must actually parse onto ScreenContextPayload.

    ScreenContextPayload has no alias_generator/populate_by_name and no
    `mode` field today — a posted `mode` is silently dropped by Pydantic
    v2's default extra='ignore'. Use getattr with a sentinel so a missing
    field surfaces as a clean assertion failure (RED), not an AttributeError.
    """
    ctx = ScreenContextPayload(page="study_session", mode="browse")
    assert getattr(ctx, "mode", "__MISSING_FIELD__") == "browse"

    ctx_default = ScreenContextPayload(page="study_session")
    assert getattr(ctx_default, "mode", "__MISSING_FIELD__") is None


# --- MODE-06: mode-aware context injection omits SM2/grading language --- #
#
# NOTE on RED-ness: as of today (pre-31-04), _build_context_injection never
# reads ctx.mode at all — so the forbidden-word absence assertions below would
# trivially pass regardless of implementation (agent.py never mentions "SM2"/
# "grading"/"scheduling" anywhere yet). The genuinely failing assertion is the
# same-vs-different check: today mode has NO effect on the injected string, so
# a browse/cram injection is byte-for-byte identical to the study-mode
# injection (RED). Once 31-04 adds the mode-aware read-only note, the two
# will diverge (GREEN) while still never containing the forbidden words.
def test_context_injection_omits_grading_language_for_browse():
    ctx_browse = ScreenContextPayload(
        page="study_session", mode="browse", card_index=1, total_cards=5
    )
    ctx_study = ScreenContextPayload(
        page="study_session", mode="study", card_index=1, total_cards=5
    )
    injection_browse = _build_context_injection(ctx_browse)
    injection_study = _build_context_injection(ctx_study)
    lowered = injection_browse.lower()
    assert "sm2" not in lowered
    assert "grading" not in lowered
    assert "grade" not in lowered
    assert "scheduling" not in lowered
    assert injection_browse != injection_study


def test_context_injection_omits_grading_language_for_cram():
    ctx_cram = ScreenContextPayload(
        page="study_session", mode="cram", card_index=1, total_cards=5
    )
    ctx_study = ScreenContextPayload(
        page="study_session", mode="study", card_index=1, total_cards=5
    )
    injection_cram = _build_context_injection(ctx_cram)
    injection_study = _build_context_injection(ctx_study)
    lowered = injection_cram.lower()
    assert "sm2" not in lowered
    assert "grading" not in lowered
    assert "grade" not in lowered
    assert "scheduling" not in lowered
    assert injection_cram != injection_study


def test_context_injection_unaffected_for_study_mode():
    """mode='study' (and mode=None) must NOT carry a browse/cram note."""
    ctx_study = ScreenContextPayload(
        page="study_session", mode="study", card_index=1, total_cards=5
    )
    ctx_none = ScreenContextPayload(
        page="study_session", card_index=1, total_cards=5
    )
    injection_study = _build_context_injection(ctx_study)
    injection_none = _build_context_injection(ctx_none)
    assert injection_study == injection_none


# --- MODE-07: session_summary wording (Pitfall 3, D-06) --- #
def test_session_summary_template_uses_needs_attention_framing():
    """The LLM prompt template must drop 'wrong'/'missed' wording and use
    'attention' framing instead (part A of the dual-path wording change)."""
    template = _INTERVENTION_PROMPT_TEMPLATES["session_summary"]
    lowered = template.lower()
    assert "wrong" not in lowered
    assert "missed" not in lowered
    assert "attention" in lowered


def test_session_summary_fallback_uses_needs_attention_framing():
    """The rule-based fallback message must drop 'wrong'/'missed' wording in
    both the zero-count and multi-miss branches (part B of Pitfall 3), and
    the zero-count message stays diagnostic (no '!', no praise word) per
    D-06 — numeric consistency, not tonal consistency."""
    zero_wrong_body = InterventionRequest(
        type="session_summary",
        session_total_cards=10,
        session_wrong_count=0,
    )
    zero_wrong_message = _build_session_summary_message(zero_wrong_body)
    lowered_zero = zero_wrong_message.lower()
    assert "wrong" not in lowered_zero
    assert "missed" not in lowered_zero
    assert "!" not in zero_wrong_message
    assert "perfect" not in lowered_zero
    assert "great" not in lowered_zero

    multi_miss_body = InterventionRequest(
        type="session_summary",
        session_total_cards=10,
        session_wrong_count=3,
        most_missed_card_front="Photosynthesis",
    )
    multi_miss_message = _build_session_summary_message(multi_miss_body)
    lowered_multi = multi_miss_message.lower()
    assert "wrong" not in lowered_multi
    assert "missed" not in lowered_multi
