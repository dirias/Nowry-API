"""AI-001: which failures move to the next provider, and how long a provider stays down."""
from unittest.mock import patch

import groq
import httpx
import pytest
from google.api_core import exceptions as google_exc

from app.ai_orchestrator.llm_clients.gemini_client import GeminiQuotaError, GeminiTransientError
from app.core import llm_health


def _groq_status(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return cls("groq said no", response=httpx.Response(status, request=request), body=None)


@pytest.fixture(autouse=True)
def _clean_breaker():
    llm_health.reset()
    yield
    llm_health.reset()


@pytest.mark.parametrize(
    "exc",
    [
        google_exc.ResourceExhausted("Your prepayment credits are depleted."),
        google_exc.ServiceUnavailable("down"),
        google_exc.InternalServerError("boom"),
        google_exc.DeadlineExceeded("slow"),
        _groq_status(groq.RateLimitError, 429),
        _groq_status(groq.InternalServerError, 500),
        _groq_status(groq.APIStatusError, 503),
        groq.APITimeoutError(request=httpx.Request("POST", "https://api.groq.com")),
        GeminiQuotaError("quota"),
        GeminiTransientError("5xx"),
        TimeoutError(),
    ],
)
def test_provider_failures_move_on(exc: Exception) -> None:
    assert llm_health.is_provider_failure(exc)


@pytest.mark.parametrize(
    "exc",
    [
        google_exc.InvalidArgument("bad request"),
        google_exc.PermissionDenied("bad key"),
        _groq_status(groq.BadRequestError, 400),
        _groq_status(groq.AuthenticationError, 401),
        ValueError("blocked by safety"),
    ],
)
def test_request_and_config_failures_do_not(exc: Exception) -> None:
    assert not llm_health.is_provider_failure(exc)


def test_a_wrapped_quota_error_is_still_quota() -> None:
    try:
        try:
            raise google_exc.ResourceExhausted("depleted")
        except google_exc.ResourceExhausted as inner:
            raise RuntimeError("Gemini API call failed") from inner
    except RuntimeError as outer:
        assert llm_health.is_quota_failure(outer)
        assert llm_health.is_provider_failure(outer)


def test_quota_keeps_a_provider_down_for_ten_minutes() -> None:
    with patch("app.core.llm_health.time.monotonic", return_value=1000.0):
        llm_health.mark_down("gemini", google_exc.ResourceExhausted("depleted"))
    with patch("app.core.llm_health.time.monotonic", return_value=1599.0):
        assert llm_health.is_down("gemini")
    with patch("app.core.llm_health.time.monotonic", return_value=1600.0):
        assert not llm_health.is_down("gemini")


def test_a_transient_failure_keeps_it_down_for_a_minute() -> None:
    with patch("app.core.llm_health.time.monotonic", return_value=1000.0):
        llm_health.mark_down("groq", google_exc.ServiceUnavailable("down"))
    with patch("app.core.llm_health.time.monotonic", return_value=1059.0):
        assert llm_health.is_down("groq")
    with patch("app.core.llm_health.time.monotonic", return_value=1060.0):
        assert not llm_health.is_down("groq")


def test_a_provider_never_marked_is_up() -> None:
    assert not llm_health.is_down("gemini")
