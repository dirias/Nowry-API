"""
Shared Langfuse observability client singleton.

Initialized at app startup (module-level) if LANGFUSE_SECRET_KEY and
LANGFUSE_PUBLIC_KEY are set. Available app-wide via dependency injection
using Depends(get_langfuse_client).

If env vars are absent or initialization fails, returns None. All callers
MUST guard usage with: if client: client.observe(...)

Usage:
    from fastapi import Depends
    from app.core.langfuse_client import get_langfuse_client

    @router.post("/endpoint")
    async def my_endpoint(
        client: Optional[Langfuse] = Depends(get_langfuse_client)
    ):
        if client:
            with client.observe(...) as obs:
                result = await llm_call()
                obs.end(output=result)
        else:
            result = await llm_call()  # no tracing
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level env var reads — must happen before client creation
_LANGFUSE_SECRET_KEY: Optional[str] = os.getenv("LANGFUSE_SECRET_KEY")
_LANGFUSE_PUBLIC_KEY: Optional[str] = os.getenv("LANGFUSE_PUBLIC_KEY")
_LANGFUSE_BASE_URL_RAW: str = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

# T-9-03 mitigation: validate LANGFUSE_BASE_URL starts with https:// to prevent MITM
_LANGFUSE_DEFAULT_URL = "https://cloud.langfuse.com"
if not _LANGFUSE_BASE_URL_RAW.startswith("https://"):
    logger.warning(
        "LANGFUSE_BASE_URL '%s' does not start with https:// — falling back to default '%s'. "
        "Insecure URLs are rejected to prevent MITM attacks.",
        _LANGFUSE_BASE_URL_RAW,
        _LANGFUSE_DEFAULT_URL,
    )
    _LANGFUSE_BASE_URL: str = _LANGFUSE_DEFAULT_URL
else:
    _LANGFUSE_BASE_URL = _LANGFUSE_BASE_URL_RAW

# Module-level singleton — initialized once at import time (before FastAPI app creation)
# Mirrors the Sentry init pattern in app/main.py (lines 7-21): env-gated, try/except, no raise
_langfuse_client = None

if _LANGFUSE_SECRET_KEY and _LANGFUSE_PUBLIC_KEY:
    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            secret_key=_LANGFUSE_SECRET_KEY,
            public_key=_LANGFUSE_PUBLIC_KEY,
            base_url=_LANGFUSE_BASE_URL,
            # SDK defaults: propagate_exceptions=False (no crash on network errors)
        )
        logger.info("Langfuse client initialized successfully.")
    except Exception as e:
        logger.warning(
            "Failed to initialize Langfuse client: %s. Continuing without tracing.",
            e,
        )
        _langfuse_client = None
else:
    logger.warning(
        "Langfuse disabled — LANGFUSE_SECRET_KEY not set. "
        "Tracing and prompt management will use local fallback."
    )


def get_langfuse_client():
    """
    Dependency injection function for FastAPI routes.
    Returns the Langfuse client singleton, or None if disabled/unavailable.

    The client is cached at module load time; all requests receive the same instance.
    If initialization failed or env vars are missing, returns None.
    All callers MUST guard usage: if client: client.observe(...)

    Usage in router:
        @router.post("/quiz")
        async def gen_quiz(
            client = Depends(get_langfuse_client)
        ):
            if client:
                with client.observe(...) as obs:
                    result = await llm_call()
                    obs.end(output=result)
            else:
                result = await llm_call()  # no tracing
    """
    return _langfuse_client
