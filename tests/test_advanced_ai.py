"""
Phase 6 Advanced AI Feature Tests
Wave 0: Named stubs — implement each in Wave 1 (replace pytest.skip with real assertions).

Covers:
  ILLUS-01: Free tier illustration cap (2/book)
  ILLUS-02: Plus tier illustration (no cap)
  ILLUS-03: Pro tier illustration (no cap)
  TTS-01: Plus tier TTS returns MP3 bytes
  TTS-02: Pro tier TTS accepts language_code
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def _ensure_advanced_ai_importable() -> None:
    """Stub out google.cloud.texttospeech and other missing deps so the
    routers can be imported on Python 3.9 test runner without credentials."""
    if "groq" not in sys.modules:
        sys.modules["groq"] = MagicMock()
    if "google" not in sys.modules:
        sys.modules["google"] = MagicMock()
    if "google.generativeai" not in sys.modules:
        sys.modules["google.generativeai"] = MagicMock()
    # Phase 6 — Google Cloud TTS stubs:
    if "google.cloud" not in sys.modules:
        sys.modules["google.cloud"] = MagicMock()
    if "google.cloud.texttospeech" not in sys.modules:
        sys.modules["google.cloud.texttospeech"] = MagicMock()
    if "google.oauth2" not in sys.modules:
        sys.modules["google.oauth2"] = MagicMock()
    if "google.oauth2.service_account" not in sys.modules:
        sys.modules["google.oauth2.service_account"] = MagicMock()
    mock_firebase = MagicMock()
    mock_firebase.get_firebase_user = MagicMock()
    sys.modules.setdefault("app.auth.firebase_auth", mock_firebase)
    mock_deps = MagicMock()
    mock_deps.track_ai_usage = MagicMock()
    mock_deps.get_subscription_tier = MagicMock()
    sys.modules.setdefault("app.auth.dependencies", mock_deps)
    # Stub orchestrator to prevent langgraph / LangChain import errors on test runner
    # (same pattern as _ensure_cards_importable in test_ai_magic.py)
    if "app.ai_orchestrator.orchestrator" not in sys.modules:
        sys.modules["app.ai_orchestrator.orchestrator"] = MagicMock()
    if "app.ai_orchestrator.llm_clients.gemini_client" not in sys.modules:
        sys.modules["app.ai_orchestrator.llm_clients.gemini_client"] = MagicMock()
    if "app.ai_orchestrator.llm_clients.tts_client" not in sys.modules:
        sys.modules["app.ai_orchestrator.llm_clients.tts_client"] = MagicMock()


_ensure_advanced_ai_importable()


# ─────────────────────────────────────────────────────────────────────────────
# ILLUS-01: POST /book/{book_id}/diagram — Free tier cap (2/book)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagram_free_tier_cap(mock_book_doc_with_counter):
    """ILLUS-01: Free tier with illustration_count >= 2 → 403 Forbidden."""
    from fastapi import HTTPException
    from unittest.mock import patch, AsyncMock
    from app.models.illustration import DiagramRequest
    from app.routers.illustrations import generate_diagram

    # Set illustration_count to cap limit
    mock_book_doc_with_counter["illustration_count"] = 2

    body = DiagramRequest(selected_text="Some text about photosynthesis")
    user = {
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "free"},
    }

    with patch("app.routers.illustrations.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        with pytest.raises(HTTPException) as exc_info:
            await generate_diagram(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=user,
            )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_diagram_does_not_increment_counter_on_generate(mock_book_doc_with_counter):
    """ILLUS-01: generate_diagram does NOT increment illustration_count (count-on-insert fix,
    commit eaa8c7f). Counter increments only when user clicks 'Insert into Book' via
    POST /book/{id}/diagram/confirm."""
    from unittest.mock import patch, AsyncMock
    from app.models.illustration import DiagramRequest
    from app.routers.illustrations import generate_diagram

    # illustration_count=0 (default fixture) — within free tier cap
    body = DiagramRequest(selected_text="Explain the water cycle")
    user = {
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "free"},
    }

    with patch("app.routers.illustrations.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        mock_col.update_one = AsyncMock()
        with patch(
            "app.routers.illustrations.orchestrator.invoke",
            return_value={"mermaid_code": "graph TD; A-->B", "explanation": "test"},
        ):
            await generate_diagram(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=user,
            )

    # generate_diagram must NOT call update_one — counter is deferred to confirm endpoint
    assert mock_col.update_one.call_count == 0


@pytest.mark.asyncio
async def test_confirm_diagram_increments_counter(mock_book_doc_with_counter):
    """ILLUS-01: POST /book/{id}/diagram/confirm increments illustration_count via $inc.
    This is the count-on-insert endpoint — called by the frontend only when the user
    actually clicks 'Insert into Book' in DiagramPreviewPanel."""
    from unittest.mock import patch, AsyncMock
    from app.routers.illustrations import confirm_diagram_insert

    # illustration_count=0 (default fixture) — within free tier cap
    user = {
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "free"},
    }

    with patch("app.routers.illustrations.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        mock_col.update_one = AsyncMock()
        result = await confirm_diagram_insert(
            book_id="60b8d295f1d2c17f4e4b1234",
            current_user=user,
        )

    # confirm endpoint must call update_one with $inc: {illustration_count: 1}
    assert mock_col.update_one.call_count == 1
    call_args = mock_col.update_one.call_args
    assert call_args[0][1] == {"$inc": {"illustration_count": 1}}
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_confirm_diagram_free_tier_cap(mock_book_doc_with_counter):
    """ILLUS-01: confirm_diagram_insert returns 403 when Free tier is already at cap.
    Race-condition guard — a concurrent session could fill the last slot between
    generate and insert."""
    from fastapi import HTTPException
    from unittest.mock import patch, AsyncMock
    from app.routers.illustrations import confirm_diagram_insert

    # Set illustration_count to cap limit
    mock_book_doc_with_counter["illustration_count"] = 2

    user = {
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "free"},
    }

    with patch("app.routers.illustrations.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        mock_col.update_one = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await confirm_diagram_insert(
                book_id="60b8d295f1d2c17f4e4b1234",
                current_user=user,
            )

    assert exc_info.value.status_code == 403
    # update_one must NOT be called when cap is reached
    assert mock_col.update_one.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# ILLUS-02: Plus tier — no per-book cap
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagram_plus_no_cap(mock_book_doc_with_counter, mock_user_doc_plus):
    """ILLUS-02: Plus tier with illustration_count >= 2 → still succeeds (no cap for Plus)."""
    from unittest.mock import patch, AsyncMock
    from app.models.illustration import DiagramRequest, DiagramResponse
    from app.routers.illustrations import generate_diagram

    # Set illustration_count above free tier cap
    mock_book_doc_with_counter["illustration_count"] = 5

    body = DiagramRequest(selected_text="Explain the Krebs cycle")
    user = {
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "plus"},
    }

    with patch("app.routers.illustrations.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        mock_col.update_one = AsyncMock()
        with patch(
            "app.routers.illustrations.orchestrator.invoke",
            return_value={"mermaid_code": "graph TD; A-->B", "explanation": "Krebs cycle"},
        ):
            result = await generate_diagram(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=user,
            )

    # Plus tier should never get 403 regardless of illustration_count
    assert isinstance(result, DiagramResponse)
    assert result.mermaid_code == "graph TD; A-->B"


# ─────────────────────────────────────────────────────────────────────────────
# ILLUS-03: Pro tier — no per-book cap
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagram_pro_no_cap(mock_book_doc_with_counter, mock_user_doc_pro):
    """ILLUS-03: Pro tier with illustration_count >= 2 → still succeeds (no cap for Pro)."""
    from unittest.mock import patch, AsyncMock
    from app.models.illustration import DiagramRequest, DiagramResponse
    from app.routers.illustrations import generate_diagram

    # Set illustration_count above free tier cap
    mock_book_doc_with_counter["illustration_count"] = 5

    body = DiagramRequest(selected_text="Explain the Big Bang theory")
    user = {
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "pro"},
    }

    with patch("app.routers.illustrations.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        mock_col.update_one = AsyncMock()
        with patch(
            "app.routers.illustrations.orchestrator.invoke",
            return_value={"mermaid_code": "graph TD; A-->B", "explanation": "Big Bang"},
        ):
            result = await generate_diagram(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=user,
            )

    # Pro tier should never get 403 regardless of illustration_count
    assert isinstance(result, DiagramResponse)
    assert result.mermaid_code == "graph TD; A-->B"


# ─────────────────────────────────────────────────────────────────────────────
# TTS-01: POST /book/{book_id}/tts — Free tier blocked, Plus tier succeeds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_free_403(mock_book_doc_with_counter):
    """TTS-01: Free tier → 403 Forbidden on POST /book/{id}/tts."""
    from fastapi import HTTPException
    from unittest.mock import patch, AsyncMock
    from app.models.tts import TTSRequest
    from app.routers.tts import generate_tts

    body = TTSRequest(text="Hello world", language_code="en-US")
    user = {"user_id": "507f1f77bcf86cd799439011"}

    with patch("app.routers.tts.get_subscription_tier", return_value="free"):
        with pytest.raises(HTTPException) as exc_info:
            await generate_tts(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                tier="free",
                current_user=user,
            )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_tts_plus_returns_audio(mock_book_doc_with_counter, mock_user_doc_plus, mock_tts_client):
    """TTS-01: Plus tier → 200 with audio/mpeg content (mocked Google Cloud TTS)."""
    from unittest.mock import patch, AsyncMock
    from app.models.tts import TTSRequest
    from app.routers.tts import generate_tts

    body = TTSRequest(text="Hello world", language_code="en-US")
    user = {"user_id": "507f1f77bcf86cd799439011"}

    with patch("app.routers.tts.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        with patch("app.routers.tts.get_tts_client", return_value=mock_tts_client):
            response = await generate_tts(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                tier="plus",
                current_user=user,
            )

    assert response.media_type == "audio/mpeg"
    assert response.body == b"fake-mp3-bytes"


# ─────────────────────────────────────────────────────────────────────────────
# TTS-02: Pro tier — language_code param used
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_pro_language_code(mock_book_doc_with_counter, mock_user_doc_pro, mock_tts_client):
    """TTS-02: Pro tier with language_code='fr-FR' → synthesize_speech called with fr-FR."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from app.models.tts import TTSRequest
    from app.routers.tts import generate_tts

    body = TTSRequest(text="Bonjour le monde", language_code="fr-FR")
    user = {"user_id": "507f1f77bcf86cd799439011"}

    mock_texttospeech = MagicMock()

    with patch("app.routers.tts.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        with patch("app.routers.tts.get_tts_client", return_value=mock_tts_client):
            with patch("app.routers.tts.texttospeech", mock_texttospeech):
                await generate_tts(
                    book_id="60b8d295f1d2c17f4e4b1234",
                    body=body,
                    tier="pro",
                    current_user=user,
                )

    # Verify VoiceSelectionParams was called with language_code='fr-FR'
    voice_call_kwargs = mock_texttospeech.VoiceSelectionParams.call_args.kwargs
    assert voice_call_kwargs.get("language_code") == "fr-FR"


@pytest.mark.asyncio
async def test_tts_plus_ignores_language(mock_book_doc_with_counter, mock_user_doc_plus, mock_tts_client):
    """TTS-02: Plus tier: language_code param in body is ignored → always uses en-US."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from app.models.tts import TTSRequest
    from app.routers.tts import generate_tts

    body = TTSRequest(text="こんにちは世界", language_code="ja-JP")
    user = {"user_id": "507f1f77bcf86cd799439011"}

    mock_texttospeech = MagicMock()

    with patch("app.routers.tts.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=mock_book_doc_with_counter)
        with patch("app.routers.tts.get_tts_client", return_value=mock_tts_client):
            with patch("app.routers.tts.texttospeech", mock_texttospeech):
                await generate_tts(
                    book_id="60b8d295f1d2c17f4e4b1234",
                    body=body,
                    tier="plus",
                    current_user=user,
                )

    # Verify VoiceSelectionParams was called with en-US (not ja-JP) for Plus tier
    voice_call_kwargs = mock_texttospeech.VoiceSelectionParams.call_args.kwargs
    assert voice_call_kwargs.get("language_code") == "en-US"
    assert voice_call_kwargs.get("language_code") != "ja-JP"
