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


_ensure_advanced_ai_importable()


# ─────────────────────────────────────────────────────────────────────────────
# ILLUS-01: POST /book/{book_id}/diagram — Free tier cap (2/book)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagram_free_tier_cap(mock_book_doc_with_counter):
    """ILLUS-01: Free tier with illustration_count >= 2 → 403 Forbidden."""
    pytest.skip("Wave 0 stub — implement after illustrations.py endpoint is built")


@pytest.mark.asyncio
async def test_diagram_increments_counter(mock_book_doc_with_counter):
    """ILLUS-01: Successful diagram generation increments illustration_count via $inc."""
    pytest.skip("Wave 0 stub — implement after illustrations.py endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# ILLUS-02: Plus tier — no per-book cap
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagram_plus_no_cap(mock_book_doc_with_counter, mock_user_doc_plus):
    """ILLUS-02: Plus tier with illustration_count >= 2 → still succeeds (no cap for Plus)."""
    pytest.skip("Wave 0 stub — implement after illustrations.py endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# ILLUS-03: Pro tier — no per-book cap
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagram_pro_no_cap(mock_book_doc_with_counter, mock_user_doc_pro):
    """ILLUS-03: Pro tier with illustration_count >= 2 → still succeeds (no cap for Pro)."""
    pytest.skip("Wave 0 stub — implement after illustrations.py endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# TTS-01: POST /book/{book_id}/tts — Free tier blocked, Plus tier succeeds
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_free_403(mock_book_doc_with_counter):
    """TTS-01: Free tier → 403 Forbidden on POST /book/{id}/tts."""
    pytest.skip("Wave 0 stub — implement after tts.py endpoint is built")


@pytest.mark.asyncio
async def test_tts_plus_returns_audio(mock_book_doc_with_counter, mock_user_doc_plus, mock_tts_client):
    """TTS-01: Plus tier → 200 with audio/mpeg content (mocked Google Cloud TTS)."""
    pytest.skip("Wave 0 stub — implement after tts.py endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# TTS-02: Pro tier — language_code param used
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_pro_language_code(mock_book_doc_with_counter, mock_user_doc_pro, mock_tts_client):
    """TTS-02: Pro tier with language_code='fr-FR' → synthesize_speech called with fr-FR."""
    pytest.skip("Wave 0 stub — implement after tts.py endpoint is built")


@pytest.mark.asyncio
async def test_tts_plus_ignores_language(mock_book_doc_with_counter, mock_user_doc_plus, mock_tts_client):
    """TTS-02: Plus tier: language_code param in body is ignored → always uses en-US."""
    pytest.skip("Wave 0 stub — implement after tts.py endpoint is built")
