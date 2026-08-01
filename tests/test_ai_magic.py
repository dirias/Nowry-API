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

def _ensure_cards_importable():
    """
    Install sys.modules stubs for cards.py import chain.
    Mirrors _ensure_books_importable — cards.py also imports firebase_auth.
    """
    _ensure_books_importable()
    # Stub orchestrator to prevent langgraph / LangChain import errors
    if "app.ai_orchestrator.orchestrator" not in sys.modules:
        mock_orch = MagicMock()
        sys.modules["app.ai_orchestrator.orchestrator"] = mock_orch
    if "app.ai_orchestrator.llm_clients.gemini_client" not in sys.modules:
        mock_gemini_mod = MagicMock()
        sys.modules["app.ai_orchestrator.llm_clients.gemini_client"] = mock_gemini_mod
    # Stub subscription_plans to avoid SubscriptionTier import issues in quiz_ai
    if "app.config.subscription_plans" not in sys.modules:
        mock_plans = MagicMock()
        sys.modules["app.config.subscription_plans"] = mock_plans
    # Stub slowapi + app.core.limiter to unblock quiz_ai.py import on CI (no slowapi installed)
    if "slowapi" not in sys.modules:
        sys.modules["slowapi"] = MagicMock()
    if "slowapi.util" not in sys.modules:
        sys.modules["slowapi.util"] = MagicMock()
    if "app.core.limiter" not in sys.modules:
        mock_limiter_mod = MagicMock()
        sys.modules["app.core.limiter"] = mock_limiter_mod
    # Stub app.models.quiz — contains str|None Pydantic fields that fail on Python 3.9
    # Must provide real Pydantic models so FastAPI router @response_model works at import time
    if "app.models.quiz" not in sys.modules:
        from typing import Optional, Literal as _Literal
        from pydantic import BaseModel as _BM, Field as _Field

        class _AIQuizQuestionResponse(_BM):
            card_id: str
            question_type: str
            question_text: str
            options: Optional[list] = None
            hint_available: bool = True
            card_index: int

        class _AIQuizQuestionStored(_BM):
            card_id: str
            question_type: str
            question_text: str
            options: Optional[list] = None
            hint_available: bool = True
            correct_answer: str
            rubric: str = ""
            card_index: int

        class _AIQuizStartRequest(_BM):
            topic: Optional[str] = _Field(default=None, max_length=200)
            question_count: int = _Field(default=10, ge=1, le=20)
            language: str = _Field(default="en", max_length=10)

        class _AIQuizStartResponse(_BM):
            session_id: str
            total_questions: int
            first_question: _AIQuizQuestionResponse

        mock_quiz_mod = MagicMock()
        mock_quiz_mod.AIQuizQuestionResponse = _AIQuizQuestionResponse
        mock_quiz_mod.AIQuizQuestionStored = _AIQuizQuestionStored
        mock_quiz_mod.AIQuizStartRequest = _AIQuizStartRequest
        mock_quiz_mod.AIQuizStartResponse = _AIQuizStartResponse
        sys.modules["app.models.quiz"] = mock_quiz_mod


_ensure_cards_importable()


@pytest.mark.asyncio
async def test_generate_from_book_free_403(mock_books_collection, mock_user_doc):
    """CARD-02: Free tier → 403 Forbidden on POST /card/generate-from-book."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.models.book_generation import GenerateFromBookRequest
    from app.routers.cards import generate_cards_from_book

    user_doc = dict(mock_user_doc)
    user_doc["user_id"] = "507f1f77bcf86cd799439011"

    body = GenerateFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    with pytest.raises(HTTPException) as exc_info:
        await generate_cards_from_book(
            body=body,
            current_user=user_doc,
            tier="free",
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_generate_from_book_plus_success(mock_books_collection, mock_user_doc_plus):
    """CARD-02: Plus tier → 200 with non-empty cards array."""
    import json as _json
    from unittest.mock import patch
    from app.models.book_generation import GenerateFromBookRequest
    from app.routers.cards import generate_cards_from_book

    body = GenerateFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "full_content": _json.dumps({
            "root": {"children": [{"type": "paragraph", "children": [
                {"type": "text", "text": "Study content for plus tier test."}
            ]}]}
        }),
        "deleted_at": None,
    }

    cards_json = _json.dumps([
        {"title": "What is photosynthesis?", "content": "The process by which plants make food from sunlight."},
        {"title": "Define osmosis", "content": "Movement of water across a semipermeable membrane."},
    ])

    mock_gemini_instance = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = cards_json
    mock_gemini_instance.request.return_value = mock_shim

    with patch("app.routers.cards.books_collection") as mock_books_col:
        mock_books_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.cards.Gemini_client", return_value=mock_gemini_instance):
            result = await generate_cards_from_book(
                body=body,
                current_user=mock_user_doc_plus,
                tier="plus",
            )

    assert len(result.cards) > 0
    assert result.cards[0].title != ""


# ─────────────────────────────────────────────────────────────────────────────
# CARD-03: POST /card/analyze-deck — Pro only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyze_deck_non_pro_403(mock_deck_with_cards, mock_user_doc, mock_user_doc_plus):
    """CARD-03: Free and Plus tier → 403 Forbidden on POST /card/analyze-deck."""
    from fastapi import HTTPException
    from app.models.deck_analysis import DeckAnalysisRequest
    from app.routers.cards import analyze_deck

    deck_id = str(mock_deck_with_cards["deck"]["_id"])
    body = DeckAnalysisRequest(deck_id=deck_id)

    # Free tier → 403
    user_doc_free = dict(mock_user_doc)
    user_doc_free["user_id"] = "507f1f77bcf86cd799439011"

    with pytest.raises(HTTPException) as exc_free:
        await analyze_deck(body=body, current_user=user_doc_free, tier="free")
    assert exc_free.value.status_code == 403

    # Plus tier → 403
    with pytest.raises(HTTPException) as exc_plus:
        await analyze_deck(body=body, current_user=mock_user_doc_plus, tier="plus")
    assert exc_plus.value.status_code == 403


@pytest.mark.asyncio
async def test_analyze_deck_pro_success(mock_deck_with_cards, mock_user_doc_pro):
    """CARD-03: Pro tier → 200 with duplicates, gaps, rewrite_suggestions keys."""
    import json as _json
    from unittest.mock import patch, MagicMock, AsyncMock
    from bson import ObjectId
    from app.models.deck_analysis import DeckAnalysisRequest
    from app.routers.cards import analyze_deck

    deck_data = mock_deck_with_cards
    deck_doc = deck_data["deck"]
    card_docs = deck_data["cards"]
    deck_id_str = str(deck_doc["_id"])

    body = DeckAnalysisRequest(deck_id=deck_id_str)

    analysis_json = _json.dumps({
        "duplicates": [
            {
                "card_a_id": str(card_docs[0]["_id"]),
                "card_b_id": str(card_docs[1]["_id"]),
                "reason": "Both cards cover the same concept.",
            }
        ],
        "gaps": [
            {"topic": "Memory techniques", "description": "No cards covering spaced repetition."}
        ],
        "rewrite_suggestions": [
            {
                "card_id": str(card_docs[2]["_id"]),
                "original_front": card_docs[2]["title"],
                "original_back": card_docs[2]["content"],
                "suggested_front": "What is Card 2?",
                "suggested_back": "An improved explanation of Card 2.",
                "reason": "The original phrasing is ambiguous.",
            }
        ],
    })

    mock_gemini_instance = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = analysis_json
    mock_gemini_instance.request.return_value = mock_shim

    # Mock cards_collection.find(...).skip(...).to_list(...)
    mock_cursor = MagicMock()
    mock_cursor.skip.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(side_effect=[card_docs, []])  # first batch: cards, second: empty

    with patch("app.routers.cards.decks_collection") as mock_decks_col:
        mock_decks_col.find_one = AsyncMock(return_value=deck_doc)
        with patch("app.routers.cards.cards_collection") as mock_cards_col:
            mock_cards_col.find.return_value = mock_cursor
            with patch("app.routers.cards.Gemini_client", return_value=mock_gemini_instance):
                result = await analyze_deck(
                    body=body,
                    current_user=mock_user_doc_pro,
                    tier="pro",
                )

    assert hasattr(result, "duplicates")
    assert hasattr(result, "gaps")
    assert hasattr(result, "rewrite_suggestions")
    assert len(result.duplicates) == 1
    assert len(result.gaps) == 1
    assert len(result.rewrite_suggestions) == 1


# ─────────────────────────────────────────────────────────────────────────────
# QUIZ-01: quiz_ai.py Free cap 10 → 5
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiz_free_cap_5():
    """QUIZ-01: Free tier quiz generation returns at most 5 questions (cap changed from 10)."""
    from app.routers.quiz_ai import _resolve_question_count

    # With no explicit count — free tier always returns 5
    # Note: signature is _resolve_question_count(requested, user, tier)
    user_doc = {"subscription": {"tier": "free"}, "preferences": {}}
    assert _resolve_question_count(5, user_doc, "free") == 5
    # With a high explicit count — still capped at 5
    assert _resolve_question_count(10, user_doc, "free") == 5
    assert _resolve_question_count(20, user_doc, "free") == 5


# ─────────────────────────────────────────────────────────────────────────────
# QUIZ-02: POST /quiz/generate-from-book — Plus+ only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_quiz_from_book_free_403(mock_books_collection):
    """QUIZ-02: Free tier → 403 Forbidden on POST /quiz/generate-from-book."""
    from fastapi import HTTPException
    from unittest.mock import patch
    from app.models.book_generation import GenerateQuizFromBookRequest
    from app.routers.quiz_ai import generate_quiz_from_book

    user_doc = {
        "_id": "507f1f77bcf86cd799439011",
        "user_id": "507f1f77bcf86cd799439011",
        "subscription": {"tier": "free"},
    }
    body = GenerateQuizFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    with pytest.raises(HTTPException) as exc_info:
        await generate_quiz_from_book(
            body=body,
            current_user=user_doc,
            tier="free",
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_generate_quiz_from_book_plus_success(mock_books_collection, mock_user_doc_plus):
    """QUIZ-02: Plus tier → 200 with non-empty questions array."""
    import json as _json
    from unittest.mock import patch, MagicMock, AsyncMock
    from app.models.book_generation import GenerateQuizFromBookRequest
    from app.routers.quiz_ai import generate_quiz_from_book

    body = GenerateQuizFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "full_content": _json.dumps({
            "root": {"children": [{"type": "paragraph", "children": [
                {"type": "text", "text": "Study content for plus quiz test."}
            ]}]}
        }),
        "deleted_at": None,
    }

    quiz_json = _json.dumps([
        {"question": "What is photosynthesis?", "correct_answer": "Food from sunlight",
         "incorrect_answers": ["A", "B", "C"], "difficulty": "Easy"},
        {"question": "Define osmosis?", "correct_answer": "Water across membrane",
         "incorrect_answers": ["A", "B", "C"], "difficulty": "Medium"},
    ])

    mock_gemini_instance = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = quiz_json
    mock_gemini_instance.request.return_value = mock_shim

    with patch("app.routers.quiz_ai.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.quiz_ai.get_client_for_tier", return_value=mock_gemini_instance):
            result = await generate_quiz_from_book(
                body=body,
                current_user=mock_user_doc_plus,
                tier="plus",
            )

    assert len(result.questions) == 2

    # QUIZ-02 regression: the frontend QuestionnaireModal reads
    # `options` / `answer`. The prompt emits `correct_answer` +
    # `incorrect_answers`, so the router MUST normalise — otherwise every
    # option list renders empty.
    for question in result.questions:
        assert len(question.options) == 4, "options must be materialised from correct + incorrect answers"
        assert question.answer in question.options, "the correct answer must be selectable"

    assert result.questions[0].answer == "Food from sunlight"
    assert sorted(result.questions[0].options) == sorted(["Food from sunlight", "A", "B", "C"])


@pytest.mark.asyncio
async def test_generate_quiz_from_book_live_prompt_shape_serialises_options(
    mock_books_collection, mock_user_doc_plus
):
    """QUIZ-02 regression, reproducing the reported bug exactly.

    The prompt actually served at runtime (confirmed from the prewarm
    write-through in app/config/langfuse_cache.json) asks for:

        'question', 'correct_answer', 'incorrect_answers' (list of 3), 'difficulty'

    The old router returned that verbatim, so the JSON on the wire had no
    `options` key at all and QuestionnaireModal rendered an empty list.
    This asserts on the SERIALISED body — what the browser actually receives.
    """
    import json as _json
    from unittest.mock import patch, MagicMock, AsyncMock
    from app.models.book_generation import GenerateQuizFromBookRequest
    from app.routers.quiz_ai import generate_quiz_from_book

    body = GenerateQuizFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "full_content": "JLPT N2 grammar notes.",
        "deleted_at": None,
    }

    # Verbatim live-prompt shape, with the user's real JLPT content.
    quiz_json = _json.dumps([
        {
            "question": "私の研究テーマは、日本______自動車産業の発展です。",
            "correct_answer": "における",
            "incorrect_answers": ["にとって", "によって", "につれて"],
            "difficulty": "Medium",
        },
        {
            "question": "注意事項をよく______、お申込みください。",
            "correct_answer": "お読みの上",
            "incorrect_answers": ["読むの上", "お読みのため", "読んでの上"],
            "difficulty": "Hard",
        },
    ])

    mock_client = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = quiz_json
    mock_client.request.return_value = mock_shim

    with patch("app.routers.quiz_ai.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.quiz_ai.get_client_for_tier", return_value=mock_client):
            result = await generate_quiz_from_book(
                body=body,
                current_user=mock_user_doc_plus,
                tier="plus",
            )

    # The wire body, exactly as FastAPI serialises it for the browser.
    payload = result.model_dump(mode="json")

    assert len(payload["questions"]) == 2
    for item in payload["questions"]:
        assert "options" in item, "the browser must receive an `options` key"
        assert len(item["options"]) == 4
        assert item["answer"] in item["options"]
        # QuestionnaireModal filters to non-empty strings — none may be blank.
        assert all(isinstance(o, str) and o.strip() for o in item["options"])

    first = payload["questions"][0]
    assert first["answer"] == "における"
    assert sorted(first["options"]) == sorted(["における", "にとって", "によって", "につれて"])


@pytest.mark.asyncio
async def test_generate_quiz_from_book_canonical_shape_passthrough(mock_books_collection, mock_user_doc_plus):
    """QUIZ-02: questions already in `options`/`answer` shape survive untouched,
    while entries with no usable options are dropped rather than rendered empty."""
    import json as _json
    from unittest.mock import patch, MagicMock, AsyncMock
    from app.models.book_generation import GenerateQuizFromBookRequest
    from app.routers.quiz_ai import generate_quiz_from_book

    body = GenerateQuizFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "full_content": "Plain text book body about Japanese grammar.",
        "deleted_at": None,
    }

    quiz_json = _json.dumps([
        {
            "question": "私の研究テーマは、日本______自動車産業の発展です。",
            "options": ["における", "にとって", "によって", "につれて"],
            "answer": "における",
            "explanation": "における marks a location or field.",
        },
        # Unusable: no options and no distractors to build them from.
        {"question": "Broken question", "answer": "only-answer"},
        # Unusable: no question text.
        {"options": ["a", "b", "c", "d"], "answer": "a"},
    ])

    mock_client = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = quiz_json
    mock_client.request.return_value = mock_shim

    with patch("app.routers.quiz_ai.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.quiz_ai.get_client_for_tier", return_value=mock_client):
            result = await generate_quiz_from_book(
                body=body,
                current_user=mock_user_doc_plus,
                tier="plus",
            )

    assert len(result.questions) == 1
    only = result.questions[0]
    assert only.options == ["における", "にとって", "によって", "につれて"]
    assert only.answer == "における"
    assert only.explanation == "における marks a location or field."


@pytest.mark.asyncio
async def test_generate_quiz_from_book_all_malformed_502(mock_books_collection, mock_user_doc_plus):
    """QUIZ-02: if nothing normalises into a usable question, fail loudly with
    502 instead of handing the UI a list of option-less questions."""
    import json as _json
    from unittest.mock import patch, MagicMock, AsyncMock
    from fastapi import HTTPException
    from app.models.book_generation import GenerateQuizFromBookRequest
    from app.routers.quiz_ai import generate_quiz_from_book

    body = GenerateQuizFromBookRequest(book_id="60b8d295f1d2c17f4e4b1234")

    book_doc = {
        "_id": "60b8d295f1d2c17f4e4b1234",
        "user_id": "507f1f77bcf86cd799439011",
        "full_content": "Some book text.",
        "deleted_at": None,
    }

    quiz_json = _json.dumps([{"question": "No options at all", "answer": "x"}])

    mock_client = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = quiz_json
    mock_client.request.return_value = mock_shim

    with patch("app.routers.quiz_ai.books_collection") as mock_col:
        mock_col.find_one = AsyncMock(return_value=book_doc)
        with patch("app.routers.quiz_ai.get_client_for_tier", return_value=mock_client):
            with pytest.raises(HTTPException) as exc_info:
                await generate_quiz_from_book(
                    body=body,
                    current_user=mock_user_doc_plus,
                    tier="plus",
                )

    assert exc_info.value.status_code == 502


# ─────────────────────────────────────────────────────────────────────────────
# QUIZ-03: POST /quiz/analyze-deck — Pro only
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiz_analyze_deck_free_forbidden(mock_deck_with_cards, mock_user_doc, mock_user_doc_plus):
    """QUIZ-03: Free and Plus tier → 403 Forbidden on POST /quiz/analyze-deck."""
    from fastapi import HTTPException
    from app.routers.quiz_ai import analyze_deck_for_quiz

    deck_id = str(mock_deck_with_cards["deck"]["_id"])

    # Free tier → 403
    user_doc_free = dict(mock_user_doc)
    user_doc_free["user_id"] = "507f1f77bcf86cd799439011"

    with pytest.raises(HTTPException) as exc_free:
        await analyze_deck_for_quiz(
            deck_id=deck_id,
            current_user=user_doc_free,
            tier="free",
        )
    assert exc_free.value.status_code == 403

    # Plus tier → 403
    with pytest.raises(HTTPException) as exc_plus:
        await analyze_deck_for_quiz(
            deck_id=deck_id,
            current_user=mock_user_doc_plus,
            tier="plus",
        )
    assert exc_plus.value.status_code == 403


@pytest.mark.asyncio
async def test_quiz_analyze_deck_pro_allowed(mock_deck_with_cards, mock_user_doc_pro):
    """QUIZ-03: Pro tier → 200 with non-empty quiz_questions list."""
    import json as _json
    from unittest.mock import patch, MagicMock, AsyncMock
    from app.routers.quiz_ai import analyze_deck_for_quiz

    deck_data = mock_deck_with_cards
    deck_doc = deck_data["deck"]
    card_docs = deck_data["cards"]
    deck_id_str = str(deck_doc["_id"])

    questions_json = _json.dumps(["What is Card 0?", "What is Card 1?"])

    mock_gemini_instance = MagicMock()
    mock_shim = MagicMock()
    mock_shim.choices = [MagicMock()]
    mock_shim.choices[0].message.content = questions_json
    mock_gemini_instance.request.return_value = mock_shim

    # Mock cards_collection.find(...).skip(...).to_list(...)
    mock_cursor = MagicMock()
    mock_cursor.skip.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(side_effect=[card_docs, []])

    with patch("app.routers.quiz_ai.decks_collection") as mock_decks_col:
        mock_decks_col.find_one = AsyncMock(return_value=deck_doc)
        with patch("app.routers.quiz_ai.cards_collection") as mock_cards_col:
            mock_cards_col.find.return_value = mock_cursor
            with patch("app.routers.quiz_ai.Gemini_client", return_value=mock_gemini_instance):
                result = await analyze_deck_for_quiz(
                    deck_id=deck_id_str,
                    current_user=mock_user_doc_pro,
                    tier="pro",
                )

    assert hasattr(result, "quiz_questions")
    assert len(result.quiz_questions) > 0
    assert result.card_count == len(card_docs)
    assert result.deck_id == deck_id_str
