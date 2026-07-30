"""
SSE streaming card generation tests — POST /card/generate/stream.

Follows the repo's established pattern (see test_study_cards_review_mode.py):
a minimal FastAPI app hosting only the router under test, driven through the
real HTTP layer via httpx.ASGITransport + app.dependency_overrides, keyed on
the router module's OWN already-resolved dependency references so overrides
intercept regardless of what other test modules stubbed in sys.modules.

Covers:
  1. Happy path: N `card` events, strictly increasing indices, terminal `done`,
     every data payload parses into its Pydantic model
  2. parse_error state -> single `error` event AI_MALFORMED_OUTPUT, no `card`
     events, nothing after the terminal error
  3. Quota error -> AI_QUOTA_EXHAUSTED
  4. Missing token -> plain HTTP 401 with no SSE body
  5. sampleNumber=51 -> HTTP 422 (request model bounds)
  6. Invalid raw card dropped; `total` reflects valid count only
  7. Adaptive mode (sampleNumber omitted): effective cap computed in-route,
     forwarded to the orchestrator with adaptive=True; done.mode == "auto"
  8. Over-cap model output clipped server-side; done.truncated == True
  9. excludeTitles bounds (51 entries -> 422) and forwarding to the pipeline
"""
from __future__ import annotations

import importlib
import sys
from typing import Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

# app.ai_orchestrator.orchestrator does `from langfuse.langchain import
# CallbackHandler`; conftest stubs "langfuse" but not the submodule, and the
# real package is not installed in this Python 3.9 dev venv.
sys.modules.setdefault("langfuse.langchain", MagicMock())

# Defensive vs. collection order: earlier-collected test modules
# (test_advanced_ai.py, test_ai_magic.py) stub these modules wholesale as
# MagicMocks. cards.py must bind the REAL objects (GeminiQuotaError must be a
# real exception class for `except` clauses; get_firebase_user must be the
# real dependency for the 401 test), and all of them import cleanly in this
# env, so restore the real modules before app.routers.cards is first imported.
#
# The real gemini_client needs the REAL `google` namespace package (for
# google.api_core, which IS installed) — earlier modules stub "google" itself
# as a MagicMock, which breaks `from google.api_core import exceptions`.
# Restore only the namespace roots; leaf stubs for genuinely-uninstalled
# packages ("google.generativeai", "google.cloud.texttospeech") are kept —
# sys.modules resolves them directly regardless of the parent package.
for _google_mod in ("google", "google.api_core", "google.oauth2", "google.auth"):
    if isinstance(sys.modules.get(_google_mod), MagicMock):
        del sys.modules[_google_mod]
for _mod_name in (
    "app.ai_orchestrator.llm_clients.gemini_client",
    "app.auth.firebase_auth",
    "app.auth.dependencies",
    "app.ai_orchestrator.orchestrator",
):
    if isinstance(sys.modules.get(_mod_name), MagicMock):
        del sys.modules[_mod_name]
        importlib.import_module(_mod_name)

from app.models.card_stream import (  # noqa: E402
    CardEventData,
    DoneEventData,
    ErrorEventData,
)
from app.routers import cards  # noqa: E402

USER_ID = "507f1f77bcf86cd799439011"

VALID_PAYLOAD: dict = {
    "prompt": "Make flashcards about photosynthesis",
    "sampleText": "Photosynthesis converts light energy into chemical energy.",
    "sampleNumber": 3,
}

# Minimal app hosting only the router under test — avoids app.main's full
# router set (pre-existing, unrelated import breaks on this test runner).
_test_app = FastAPI()
_test_app.include_router(cards.router)


async def _mock_firebase_user() -> dict:
    return {
        "user_id": USER_ID,
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
    }


async def _mock_track_ai_usage() -> dict:
    return {
        "user_id": USER_ID,
        "firebase_uid": "test-firebase-uid-123",
        "email": "test@example.com",
        "subscription": {"tier": "free", "status": "active", "ai_usage_count": 1},
    }


def _parse_sse(raw: str) -> list:
    """Parse an SSE stream into [(event_name, data_json_str), ...].

    Comment frames (heartbeats, lines starting with ':') are skipped.
    """
    events: list = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event_name: Optional[str] = None
        data_line: Optional[str] = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_line = line[len("data: "):]
        events.append((event_name, data_line))
    return events


async def _post_stream(payload: dict, override_auth: bool = True) -> httpx.Response:
    """POST /card/generate/stream against the minimal app.

    Overrides are keyed on cards.py's own already-resolved references
    (cards.get_firebase_user for the router-level auth dependency,
    cards.track_ai_usage for the route-level one) so they always intercept.
    """
    if override_auth:
        _test_app.dependency_overrides[cards.get_firebase_user] = _mock_firebase_user
        _test_app.dependency_overrides[cards.track_ai_usage] = _mock_track_ai_usage
    try:
        transport = httpx.ASGITransport(app=_test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/card/generate/stream", json=payload)
    finally:
        _test_app.dependency_overrides.clear()
    return response


@pytest.mark.asyncio
async def test_stream_happy_path_cards_then_done():
    """N card events, strictly increasing indices, terminal done; every data
    line parses into its Pydantic model."""
    raw_cards = [{"title": f"Front {i}", "content": f"Back {i}"} for i in range(3)]
    with patch.object(
        cards.orchestrator,
        "invoke",
        return_value={"generated_cards": raw_cards},
    ) as mock_invoke:
        response = await _post_stream(VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    mock_invoke.assert_called_once()

    events = _parse_sse(response.text)
    card_events = [e for e in events if e[0] == "card"]
    assert len(card_events) == 3

    previous_index = -1
    for _, data_line in card_events:
        parsed = CardEventData.model_validate_json(data_line)
        assert parsed.index > previous_index  # strictly increasing
        previous_index = parsed.index
        assert parsed.total == 3

    # Terminal event is done — nothing after it
    assert events[-1][0] == "done"
    done = DoneEventData.model_validate_json(events[-1][1])
    assert done.total_cards == 3
    assert done.elapsed_ms >= 0
    assert done.mode == "fixed"
    assert done.cap == 3
    assert done.truncated is False


@pytest.mark.asyncio
async def test_stream_parse_error_emits_single_malformed_error():
    """parse_error state -> single error event AI_MALFORMED_OUTPUT, no card
    events, and the error is terminal."""
    with patch.object(
        cards.orchestrator,
        "invoke",
        return_value={"generated_cards": [], "parse_error": True},
    ):
        response = await _post_stream(VALID_PAYLOAD)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert len(events) == 1
    assert events[0][0] == "error"
    error = ErrorEventData.model_validate_json(events[0][1])
    assert error.code == "AI_MALFORMED_OUTPUT"


@pytest.mark.asyncio
async def test_stream_quota_error_emits_quota_exhausted():
    """GeminiQuotaError from the pipeline task -> AI_QUOTA_EXHAUSTED."""
    with patch.object(
        cards.orchestrator,
        "invoke",
        side_effect=cards.GeminiQuotaError("quota hit"),
    ):
        response = await _post_stream(VALID_PAYLOAD)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert len(events) == 1
    assert events[0][0] == "error"
    error = ErrorEventData.model_validate_json(events[0][1])
    assert error.code == "AI_QUOTA_EXHAUSTED"


@pytest.mark.asyncio
async def test_stream_pipeline_exception_emits_pipeline_failed():
    """Any other exception (e.g. orchestrator's internal HTTPException(500))
    -> AI_PIPELINE_FAILED."""
    from fastapi import HTTPException

    with patch.object(
        cards.orchestrator,
        "invoke",
        side_effect=HTTPException(status_code=500, detail="AI pipeline failed."),
    ):
        response = await _post_stream(VALID_PAYLOAD)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert len(events) == 1
    assert events[0][0] == "error"
    error = ErrorEventData.model_validate_json(events[0][1])
    assert error.code == "AI_PIPELINE_FAILED"


@pytest.mark.asyncio
async def test_stream_missing_token_plain_401_no_sse_body():
    """Auth failures resolve to plain HTTP 401 BEFORE any SSE bytes."""
    with patch.object(cards.orchestrator, "invoke") as mock_invoke:
        response = await _post_stream(VALID_PAYLOAD, override_auth=False)

    assert response.status_code == 401
    assert not response.headers["content-type"].startswith("text/event-stream")
    assert "event:" not in response.text
    mock_invoke.assert_not_called()


@pytest.mark.asyncio
async def test_stream_sample_number_over_limit_422():
    """sampleNumber=51 violates the ge=1/le=50 bound -> HTTP 422."""
    payload = dict(VALID_PAYLOAD, sampleNumber=51)
    with patch.object(cards.orchestrator, "invoke") as mock_invoke:
        response = await _post_stream(payload)

    assert response.status_code == 422
    mock_invoke.assert_not_called()


@pytest.mark.asyncio
async def test_stream_invalid_card_dropped_total_reflects_valid_count():
    """An invalid raw card is dropped with a warning; total counts only
    valid cards."""
    raw_cards = [
        {"title": "Front 0", "content": "Back 0"},
        {"title": "Front 1"},  # missing required `content` -> dropped
        {"title": "Front 2", "content": "Back 2"},
    ]
    with patch.object(
        cards.orchestrator,
        "invoke",
        return_value={"generated_cards": raw_cards},
    ):
        response = await _post_stream(VALID_PAYLOAD)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    card_events = [e for e in events if e[0] == "card"]
    assert len(card_events) == 2

    parsed_cards = [CardEventData.model_validate_json(d) for _, d in card_events]
    assert [c.index for c in parsed_cards] == [0, 1]
    assert all(c.total == 2 for c in parsed_cards)
    assert {c.card.title for c in parsed_cards} == {"Front 0", "Front 2"}

    assert events[-1][0] == "done"
    done = DoneEventData.model_validate_json(events[-1][1])
    assert done.total_cards == 2


@pytest.mark.asyncio
async def test_stream_adaptive_mode_computes_cap_and_reports_auto():
    """sampleNumber omitted -> adaptive: the route computes the effective cap
    (short single-line text clamps to the floor of 3), forwards it to the
    orchestrator with adaptive=True, and done reports mode='auto'."""
    payload = {
        "prompt": VALID_PAYLOAD["prompt"],
        "sampleText": VALID_PAYLOAD["sampleText"],
    }
    raw_cards = [{"title": "Front 0", "content": "Back 0"}]
    with patch.object(
        cards.orchestrator,
        "invoke",
        return_value={"generated_cards": raw_cards},
    ) as mock_invoke:
        response = await _post_stream(payload)

    assert response.status_code == 200
    state = mock_invoke.call_args[0][1]
    assert state["sampleNumber"] == 3  # floor for short single-line text
    assert state["adaptive"] is True
    assert state["excludeTitles"] == []

    events = _parse_sse(response.text)
    assert events[-1][0] == "done"
    done = DoneEventData.model_validate_json(events[-1][1])
    assert done.mode == "auto"
    assert done.cap == 3
    assert done.truncated is False
    assert done.total_cards == 1


@pytest.mark.asyncio
async def test_stream_over_cap_output_clipped_and_marked_truncated():
    """Model returns more valid cards than the cap -> clipped server-side,
    done.truncated=True, card totals reflect the clipped count."""
    payload = dict(VALID_PAYLOAD, sampleNumber=2)
    raw_cards = [{"title": f"Front {i}", "content": f"Back {i}"} for i in range(4)]
    with patch.object(
        cards.orchestrator,
        "invoke",
        return_value={"generated_cards": raw_cards},
    ):
        response = await _post_stream(payload)

    assert response.status_code == 200
    events = _parse_sse(response.text)
    card_events = [e for e in events if e[0] == "card"]
    assert len(card_events) == 2
    assert all(CardEventData.model_validate_json(d).total == 2 for _, d in card_events)

    done = DoneEventData.model_validate_json(events[-1][1])
    assert done.mode == "fixed"
    assert done.cap == 2
    assert done.truncated is True
    assert done.total_cards == 2


@pytest.mark.asyncio
async def test_stream_exclude_titles_forwarded_to_pipeline():
    """excludeTitles is passed through to the orchestrator state untouched."""
    titles = ["Photosynthesis basics", "Chlorophyll"]
    payload = dict(VALID_PAYLOAD, excludeTitles=titles)
    with patch.object(
        cards.orchestrator,
        "invoke",
        return_value={"generated_cards": []},
    ) as mock_invoke:
        response = await _post_stream(payload)

    assert response.status_code == 200
    state = mock_invoke.call_args[0][1]
    assert state["excludeTitles"] == titles
    assert state["adaptive"] is False
    assert state["sampleNumber"] == 3


@pytest.mark.asyncio
async def test_stream_exclude_titles_over_limit_422():
    """51 excludeTitles entries violates max_length=50 -> HTTP 422."""
    payload = dict(VALID_PAYLOAD, excludeTitles=[f"t{i}" for i in range(51)])
    with patch.object(cards.orchestrator, "invoke") as mock_invoke:
        response = await _post_stream(payload)

    assert response.status_code == 422
    mock_invoke.assert_not_called()
