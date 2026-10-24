"""One bound on every LLM call (QA-005).

The Groq SDK defaults to 60 s with two retries, about three minutes for one
call; the Gemini SDK sets no timeout of its own. Every client is built, and
every Gemini call made, with the numbers below, so a provider that stops
answering costs a request its timeout, not the user's patience.
"""
import os

from groq import Groq


def _seconds(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, default)))
    except ValueError:
        return default


#: Long enough for a full card set from a long document on Gemini Pro.
LLM_TIMEOUT_SECONDS: float = _seconds("LLM_TIMEOUT_SECONDS", 60.0)
#: One retry on a connection error or a 5xx; a timeout is not worth two more.
LLM_MAX_RETRIES: int = 1

#: Pass as ``request_options=`` to ``generate_content`` and ``send_message``.
GEMINI_REQUEST_OPTIONS: dict = {"timeout": LLM_TIMEOUT_SECONDS}


def bounded_groq(api_key: str) -> Groq:
    """A Groq client that gives up after ``LLM_TIMEOUT_SECONDS``."""
    return Groq(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=LLM_MAX_RETRIES)
