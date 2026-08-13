"""
Centralized LLM client singletons for tier-based model routing.

Three clients (Groq/Llama 3.3 70B for Free, Gemini Flash for Plus, Gemini Pro
for Pro) are initialized once at module import time inside env-var guards.
Available app-wide via get_client_for_tier(tier).

Replaces the four duplicate _get_llm_client_for_tier() helpers that existed in
books.py, quiz_ai.py, cards.py, and orchestrator.py (MC-03).

Tier fallback (MC-02): when API keys are absent, the relevant client is None.
Call sites MUST guard usage:
    client = get_client_for_tier(tier)
    if client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable.")

TIER_MODEL_NAMES provides the Langfuse trace `model` metadata value per tier
(Phase 12, D-07) — keep this in sync with get_client_for_tier()'s tier resolution.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Class references imported once at initial load time.
# Guarded by 'not in globals()' so that importlib.reload() tests can pre-patch
# app.core.model_config.Groq_client / Gemini_client before reload — the guard
# prevents the from-import from overwriting the test's patched mock.
# See test_model_config.py: patch(...) then importlib.reload(mc) pattern.
if "Groq_client" not in globals():
    from app.ai_orchestrator.llm_clients.groq_client import Groq_client  # noqa: E402
if "Gemini_client" not in globals():
    from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client  # noqa: E402

# Module-level env var reads — must happen before client creation (mirrors langfuse_client.py:34-36)
_GROQ_API_KEY: Optional[str] = os.getenv("GROQ_API_KEY")
_GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")

_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# D-07 (11-CONTEXT.md): tier -> model name for Langfuse trace `model` metadata (Phase 12).
# Single source of truth — imported by orchestrator.py, books.py, cards.py, quiz_ai.py,
# agent.py instead of each file re-deriving its own _GROQ_MODEL/_TIER_MODEL dict (MC-03).
TIER_MODEL_NAMES: dict[str, str] = {
    "free": _GROQ_MODEL,
    "plus": "models/gemini-flash-latest",
    "pro": "models/gemini-pro-latest",
}

# Module-level singleton clients — initialized once at import time (D-10)
# Not re-created per request (that was the performance bug in the old helpers)
_groq_client = None
_gemini_flash_client = None
_gemini_pro_client = None

if _GROQ_API_KEY:
    try:
        _groq_client = Groq_client()
        logger.info("model_config: Groq client initialized (Free tier — Llama 3.3 70B).")
    except Exception as e:
        logger.warning(
            "model_config: Groq client failed to initialize: %s. Free-tier LLM unavailable.",
            e,
        )
        _groq_client = None
else:
    logger.warning(
        "model_config: GROQ_API_KEY not set — Free-tier LLM (Groq/Llama 3.3) unavailable."
    )

if _GEMINI_API_KEY:
    try:
        _gemini_flash_client = Gemini_client("models/gemini-flash-latest")
        _gemini_pro_client = Gemini_client("models/gemini-pro-latest")
        logger.info("model_config: Gemini clients initialized (Plus=Flash, Pro=Pro).")
    except Exception as e:
        logger.warning(
            "model_config: Gemini client failed to initialize: %s. Plus/Pro-tier LLM unavailable.",
            e,
        )
        _gemini_flash_client = None
        _gemini_pro_client = None
else:
    logger.warning(
        "model_config: GEMINI_API_KEY not set — Plus/Pro-tier LLM (Gemini) unavailable."
    )


def get_client_for_tier(tier: str):
    """Return the singleton LLM client for the given subscription tier.

    Resolution (D-10, D-11):
        free  → _groq_client   (Groq/Llama 3.3 70B)
        plus  → _gemini_flash_client (Gemini Flash)
        pro   → _gemini_pro_client   (Gemini Pro)
        other → WARNING + _groq_client (free-tier fallback)

    Returns:
        The singleton LLM client, or None if the relevant API key was absent
        or initialization failed. Callers MUST guard: if client is None: raise HTTPException(503)

    Args:
        tier: Subscription tier string. Expected values: 'free', 'plus', 'pro'.
    """
    if tier == "free":
        return _groq_client
    elif tier == "plus":
        return _gemini_flash_client
    elif tier == "pro":
        return _gemini_pro_client
    else:
        logger.warning(
            "model_config: Unknown tier '%s' — defaulting to free (Groq).",
            tier,
        )
        return _groq_client
