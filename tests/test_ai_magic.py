"""
Phase 5 AI Magic Feature Tests
Wave 0: Named stubs — implement each in Wave 1 (replace pytest.skip with real assertions).

Note: app.routers.books imports firebase_auth.py (Python 3.10+ dict|None syntax)
and google.generativeai (optional install). Tests use sys.modules stubs to safely
import and exercise the endpoint function — same pattern as test_stripe_webhooks.py.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, AsyncMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Module-level sys.modules stubs — installed once before any test imports books
# (mirrors the approach used for firebase_auth in test_stripe_webhooks.py)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_books_importable():
    """
    Install sys.modules stubs for packages that are not available in the test
    environment (groq, google.generativeai, firebase_auth Python-3.10-only syntax).
    Safe to call multiple times — setdefault is a no-op if already set.
    """
    if "groq" not in sys.modules:
        sys.modules["groq"] = MagicMock()
    if "google" not in sys.modules:
        sys.modules["google"] = MagicMock()
    if "google.generativeai" not in sys.modules:
        sys.modules["google.generativeai"] = MagicMock()

    mock_firebase = MagicMock()
    mock_firebase.get_firebase_user = MagicMock()
    sys.modules.setdefault("app.auth.firebase_auth", mock_firebase)

    mock_deps = MagicMock()
    mock_deps.require_ownership = MagicMock()
    mock_deps.track_ai_usage = MagicMock()
    sys.modules.setdefault("app.auth.dependencies", mock_deps)


_ensure_books_importable()


# ─────────────────────────────────────────────────────────────────────────────
# BOOK-01: POST /book/{book_id}/ai-expand — tier routing + char limits
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ai_expand_free_char_limit(mock_books_collection, mock_user_doc):
    """BOOK-01: Free tier: selected_text > 500 chars → 400 response."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.models.ai_expand import AIExpandRequest
    from app.routers.books import ai_expand_text

    user_doc = dict(mock_user_doc)
    user_doc["user_id"] = "507f1f77bcf86cd799439011"

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "deleted_at": None,
    }

    body = AIExpandRequest(selected_text="a" * 501)

    with patch("app.routers.books.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with pytest.raises(HTTPException) as exc_info:
            await ai_expand_text(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=user_doc,
            )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_ai_expand_tier_routing_free(mock_books_collection, mock_user_doc):
    """BOOK-01: Free tier uses Groq client (not Gemini) for LLM call."""
    from unittest.mock import patch
    from app.models.ai_expand import AIExpandRequest
    from app.routers.books import ai_expand_text

    user_doc = dict(mock_user_doc)
    user_doc["user_id"] = "507f1f77bcf86cd799439011"

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "deleted_at": None,
    }

    body = AIExpandRequest(selected_text="Short text for free tier test.")

    mock_groq_instance = MagicMock()
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock()]
    mock_completion.choices[0].message.content = "Expanded text from Groq"
    mock_groq_instance.chat.completions.create.return_value = mock_completion

    with patch("app.routers.books.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.books._get_groq_client", return_value=mock_groq_instance) as mock_groq_factory:
            with patch("app.routers.books.Gemini_client") as mock_gemini_cls:
                result = await ai_expand_text(
                    book_id="60b8d295f1d2c17f4e4b1234",
                    body=body,
                    current_user=user_doc,
                )

    mock_groq_factory.assert_called_once()
    mock_gemini_cls.assert_not_called()
    assert result.expanded_text == "Expanded text from Groq"


@pytest.mark.asyncio
async def test_ai_expand_tier_routing_plus(mock_books_collection, mock_user_doc_plus):
    """BOOK-02: Plus tier uses Gemini Flash client for LLM call."""
    from unittest.mock import patch
    from app.models.ai_expand import AIExpandRequest
    from app.routers.books import ai_expand_text

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "deleted_at": None,
    }

    body = AIExpandRequest(selected_text="Short text for plus tier test.")

    mock_gemini_instance = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = "Expanded text from Gemini Flash"
    mock_gemini_instance.request.return_value = mock_shim

    with patch("app.routers.books.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.books.Gemini_client", return_value=mock_gemini_instance) as mock_gemini_cls:
            with patch("app.routers.books._get_groq_client") as mock_groq_factory:
                result = await ai_expand_text(
                    book_id="60b8d295f1d2c17f4e4b1234",
                    body=body,
                    current_user=mock_user_doc_plus,
                )

    mock_gemini_cls.assert_called_once_with("models/gemini-flash-latest")
    mock_groq_factory.assert_not_called()
    assert result.expanded_text == "Expanded text from Gemini Flash"


@pytest.mark.asyncio
async def test_ai_expand_no_limit_pro(mock_books_collection, mock_user_doc_pro):
    """BOOK-03: Pro tier: no character limit enforced, request succeeds for long input."""
    from unittest.mock import patch
    from app.models.ai_expand import AIExpandRequest
    from app.routers.books import ai_expand_text

    body = AIExpandRequest(selected_text="a" * 5000)

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "deleted_at": None,
    }

    mock_gemini_instance = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = "Expanded content from Gemini Pro"
    mock_gemini_instance.request.return_value = mock_shim

    with patch("app.routers.books.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.books.Gemini_client", return_value=mock_gemini_instance):
            result = await ai_expand_text(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=mock_user_doc_pro,
            )

    assert result.expanded_text == "Expanded content from Gemini Pro"


@pytest.mark.asyncio
async def test_ai_expand_wrong_owner(mock_user_doc):
    """BOOK-01/02/03: book_id belonging to different user → 404 response."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.models.ai_expand import AIExpandRequest
    from app.routers.books import ai_expand_text

    user_doc = dict(mock_user_doc)
    user_doc["user_id"] = "507f1f77bcf86cd799439011"

    body = AIExpandRequest(selected_text="Some text to expand.")

    wrong_owner_book = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "different-user-id",
        "deleted_at": None,
    }

    with patch("app.routers.books.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=wrong_owner_book)
        with pytest.raises(HTTPException) as exc_info:
            await ai_expand_text(
                book_id="60b8d295f1d2c17f4e4b1234",
                body=body,
                current_user=user_doc,
            )

    assert exc_info.value.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# CARD-02: POST /card/generate-from-book — Plus+ only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_from_book_free_403(mock_books_collection):
    """CARD-02: Free tier → 403 Forbidden on POST /card/generate-from-book."""
    pytest.skip("Wave 0 stub — implement after cards.py generate-from-book endpoint is built")


@pytest.mark.asyncio
async def test_generate_from_book_plus_success(mock_books_collection, mock_user_doc_plus):
    """CARD-02: Plus tier → 200 with non-empty cards array."""
    pytest.skip("Wave 0 stub — implement after cards.py generate-from-book endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# CARD-03: POST /card/analyze-deck — Pro only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_deck_non_pro_403(mock_deck_with_cards):
    """CARD-03: Free and Plus tier → 403 Forbidden on POST /card/analyze-deck."""
    pytest.skip("Wave 0 stub — implement after cards.py analyze-deck endpoint is built")


@pytest.mark.asyncio
async def test_analyze_deck_pro_success(mock_deck_with_cards, mock_user_doc_pro):
    """CARD-03: Pro tier → 200 with duplicates, gaps, rewrite_suggestions keys."""
    pytest.skip("Wave 0 stub — implement after cards.py analyze-deck endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# QUIZ-01: quiz_ai.py Free cap 10 → 5
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiz_free_cap_5():
    """QUIZ-01: Free tier quiz generation returns at most 5 questions (cap changed from 10)."""
    pytest.skip("Wave 0 stub — implement after quiz_ai.py Free cap is updated")


# ─────────────────────────────────────────────────────────────────────────────
# QUIZ-02: POST /quiz/generate-from-book — Plus+ only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_quiz_from_book_free_403(mock_books_collection):
    """QUIZ-02: Free tier → 403 Forbidden on POST /quiz/generate-from-book."""
    pytest.skip("Wave 0 stub — implement after quiz_ai.py generate-from-book endpoint is built")


@pytest.mark.asyncio
async def test_generate_quiz_from_book_plus_success(mock_books_collection, mock_user_doc_plus):
    """QUIZ-02: Plus tier → 200 with non-empty questions array."""
    pytest.skip("Wave 0 stub — implement after quiz_ai.py generate-from-book endpoint is built")


# ─────────────────────────────────────────────────────────────────────────────
# QUIZ-03: POST /quiz/analyze-deck — Pro only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiz_analyze_deck_free_forbidden(mock_deck_with_cards):
    """QUIZ-03: Free and Plus tier → 403 Forbidden on POST /quiz/analyze-deck."""
    pytest.skip("Wave 0 stub — implement after quiz_ai.py analyze-deck endpoint is built")


@pytest.mark.asyncio
async def test_quiz_analyze_deck_pro_allowed(mock_deck_with_cards, mock_user_doc_pro):
    """QUIZ-03: Pro tier → 200 with non-empty quiz_questions list."""
    pytest.skip("Wave 0 stub — implement after quiz_ai.py analyze-deck endpoint is built")
