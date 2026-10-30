"""Which LLM failures belong to the provider, and which providers are down (ADR-054).

Gemini ran out of prepaid credit and every Plus and Pro chat failed for days,
while Groq answered free accounts the whole time. The fallback that fixes this
has to tell two kinds of failure apart:

* **The provider's** — quota or rate limit, a 5xx, unavailable, a timeout, no
  connection. Another provider can answer the same request, so the caller
  moves on.
* **The request's, or the config's** — a bad request, a safety block, a
  rejected key. Another model would fail the same way, or the fallback would
  hide a misconfiguration nobody would then fix. The caller raises.

A provider that has just failed is marked down for a while, so the next request
does not pay for the same failure first. Quota does not come back in seconds;
the rest usually does. The state is in process: there is one API replica, and
a restart only costs one failed call.
"""
import time

import groq
from google.api_core import exceptions as google_exc

from app.ai_orchestrator.llm_clients.gemini_client import GeminiQuotaError, GeminiTransientError

#: How long a provider is skipped after a quota failure, and after any other.
QUOTA_DOWN_SECONDS: float = 600.0
TRANSIENT_DOWN_SECONDS: float = 60.0

_QUOTA_ERRORS: tuple[type[BaseException], ...] = (
    google_exc.ResourceExhausted,
    google_exc.TooManyRequests,
    groq.RateLimitError,
    GeminiQuotaError,
)

_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    google_exc.ServiceUnavailable,
    google_exc.InternalServerError,
    google_exc.DeadlineExceeded,
    google_exc.GatewayTimeout,
    google_exc.BadGateway,
    groq.APITimeoutError,
    groq.APIConnectionError,
    groq.InternalServerError,
    GeminiTransientError,
    TimeoutError,
    ConnectionError,
)

_down_until: dict[str, float] = {}


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and what it was raised from, so a wrapped quota error still reads as one."""
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__
    return seen


def is_quota_failure(exc: BaseException) -> bool:
    """True when the provider refused for quota, credit or rate."""
    return any(isinstance(link, _QUOTA_ERRORS) for link in _chain(exc))


def is_provider_failure(exc: BaseException) -> bool:
    """True when another provider could answer the request this one failed."""
    if is_quota_failure(exc):
        return True
    for link in _chain(exc):
        if isinstance(link, _TRANSIENT_ERRORS):
            return True
        # Groq's other status errors carry the code; a 5xx is still the provider's.
        if isinstance(link, groq.APIStatusError) and link.status_code >= 500:
            return True
    return False


def mark_down(provider: str, exc: BaseException) -> None:
    """Skip ``provider`` for a while after it failed with ``exc``."""
    seconds = QUOTA_DOWN_SECONDS if is_quota_failure(exc) else TRANSIENT_DOWN_SECONDS
    _down_until[provider] = time.monotonic() + seconds


def is_down(provider: str) -> bool:
    """Whether ``provider`` failed recently enough to be skipped without a call."""
    until = _down_until.get(provider)
    if until is None:
        return False
    if time.monotonic() >= until:
        del _down_until[provider]
        return False
    return True


def reset() -> None:
    """Forget every failure — for tests."""
    _down_until.clear()
