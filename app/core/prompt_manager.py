"""
Prompt management layer for Nowry.

Serves all 8 named prompts from Langfuse at runtime with in-memory caching.
Falls back to hardcoded constants in core/prompts.py if Langfuse is unavailable.

Usage:
    from app.core import prompt_manager

    # With variables:
    text = prompt_manager.get_prompt("nowry-cards-magic", prompt=p, sample_text=s, sample_number=n)

    # No variables:
    system = prompt_manager.get_prompt("nowry-book-expand")

    # Pre-warm at startup (call from lifespan):
    await prompt_manager.prewarm()

All callers receive a ready-to-use str. They never interact with LangfusePrompt objects.
"""

import json
import logging
from pathlib import Path
from typing import Optional
import app.core.langfuse_client as _langfuse_client_module
from app.core import prompts as _fallbacks

logger = logging.getLogger(__name__)

# In-memory cache: populated by prewarm() at startup.
# Stores raw template strings (NOT compiled — compilation happens per-call with caller's vars).
# Invalidated by server restart only (no TTL in v1.1 — D-08).
_prompt_cache: dict[str, str] = {}

# Fallback mapping: prompt name -> Python format-string constant from core/prompts.py.
# Used when Langfuse is unavailable (D-05) and as the SDK fallback= argument (D-02 pitfall).
_FALLBACKS: dict[str, str] = {
    "nowry-cards-magic":    _fallbacks.RAG_CARD_GENERATION_TEMPLATE,
    "nowry-quiz-magic":     _fallbacks.QUIZ_GENERATION_TEMPLATE,
    "nowry-viz-magic":      _fallbacks.VISUALIZER_GENERATION_TEMPLATE,
    "nowry-book-expand":    _fallbacks.BOOK_EXPAND_TEMPLATE,
    "nowry-book-cards":     _fallbacks.BOOK_CARDS_TEMPLATE,
    "nowry-quiz-intent":    _fallbacks.QUIZ_INTENT_TEMPLATE,
    "nowry-quiz-from-book": _fallbacks.QUIZ_FROM_BOOK_TEMPLATE,
    "nowry-quiz-from-deck": _fallbacks.QUIZ_FROM_DECK_TEMPLATE,
}


def get_prompt(name: str, **vars) -> str:
    """Return a compiled prompt string for the given prompt name.

    Resolution order:
    1. In-memory _prompt_cache (populated by prewarm() — raw template stored, formatted here)
    2. Langfuse SDK (get_prompt + compile)
    3. Hardcoded constant from _FALLBACKS (logged WARNING)

    Args:
        name: Prompt name following the nowry-<feature> kebab-case convention (D-03, PM-03).
        **vars: Template variables substituted into the prompt. Optional (D-04).

    Returns:
        Compiled prompt string ready for use as an LLM system/user prompt.
    """
    # Serve from in-memory cache first (cache holds RAW template strings)
    if name in _prompt_cache:
        raw = _prompt_cache[name]
        return raw.format(**vars) if vars else raw

    # Try Langfuse SDK
    client = _langfuse_client_module.get_langfuse_client()
    if client:
        try:
            prompt_obj = client.get_prompt(
                name,
                type="text",
                fallback=_FALLBACKS.get(name, ""),
            )
            # Langfuse path: use .compile(**vars) which handles {{var}} mustache syntax
            if vars:
                return prompt_obj.compile(**vars)
            else:
                return prompt_obj.prompt  # raw template, no vars to substitute
        except Exception as exc:
            logger.warning(
                "[prompt_manager] Langfuse fetch failed for '%s': %s — using fallback",
                name,
                exc,
            )

    # Hardcoded fallback (D-05, D-06)
    logger.warning(
        "[prompt_manager] Langfuse unavailable — falling back to hardcoded prompt for '%s'",
        name,
    )
    fallback = _FALLBACKS.get(name, "")
    return fallback.format(**vars) if vars else fallback


async def prewarm() -> None:
    """Pre-fetch all 8 prompts into _prompt_cache and write through to langfuse_cache.json.

    Called once at FastAPI startup from the lifespan block (after create_indexes()).
    Non-raising: a failure for one prompt uses the fallback; the loop continues.
    Populates langfuse_cache.json["prompts"] for cold-start fallback (D-09).
    """
    client = _langfuse_client_module.get_langfuse_client()

    for name, fallback_template in _FALLBACKS.items():
        if client:
            try:
                prompt_obj = client.get_prompt(
                    name,
                    type="text",
                    fallback=fallback_template,
                )
                # Store RAW template (not compiled — no vars context at prewarm time — Pitfall 4)
                _prompt_cache[name] = prompt_obj.prompt
            except Exception as exc:
                logger.warning(
                    "[prompt_manager] prewarm failed for '%s': %s — using hardcoded fallback",
                    name,
                    exc,
                )
                _prompt_cache[name] = fallback_template
        else:
            _prompt_cache[name] = fallback_template

    # Also fetch model config from Langfuse and write to cache (D-12, MC-01)
    model_config_data: Optional[dict] = None
    if client:  # client was set in the loop above
        try:
            cfg_prompt = client.get_prompt("nowry-model-config", type="text")
            model_config_data = cfg_prompt.config  # dict attached to prompt version
        except Exception as exc:
            logger.warning(
                "[prompt_manager] Could not fetch model config from Langfuse: %s — using subscription_plans.py defaults",
                exc,
            )

    # Write-through to langfuse_cache.json (D-09)
    try:
        cache_path = Path(__file__).parent.parent / "config" / "langfuse_cache.json"
        with open(cache_path) as f:
            cache_data = json.load(f)
        cache_data["prompts"] = dict(_prompt_cache)
        if model_config_data is not None:
            cache_data["model_config"] = model_config_data
        cache_data["updated_at"] = None  # updated_at managed by Phase 11 sync script
        with open(cache_path, "w") as f:
            json.dump(cache_data, f, indent=2)
        logger.info("[prompt_manager] prewarm complete — %d prompts cached.", len(_prompt_cache))
    except Exception as exc:
        logger.warning(
            "[prompt_manager] Failed to write prompt cache to langfuse_cache.json: %s",
            exc,
        )
