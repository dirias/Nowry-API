"""
Phase 12 — LLM Tracing tests (TR-01..TR-06).

Covers the AI-SPEC Section 5 reference dataset (10 scenarios across 5 call sites x
2 conditions: Langfuse healthy vs. unreachable). Scenarios are added incrementally
as each call site is instrumented (12-01 through 12-05 plans).

Test isolation strategy mirrors test_langfuse_client.py / test_sync_langfuse.py:
sys.modules stubs prevent SDK import errors on the Python 3.9 test runner.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Prevent SDK import errors on Python 3.9 test runner (langfuse>=4.7.0 requires Python >=3.10;
# groq/google.generativeai/langgraph are optional/unavailable in some environments)
sys.modules.setdefault("langfuse", MagicMock())
sys.modules.setdefault("langfuse.langchain", MagicMock())
sys.modules.setdefault("groq", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())
sys.modules.setdefault("langgraph", MagicMock())
sys.modules.setdefault("langgraph.graph", MagicMock())

# When test_ai_magic.py runs earlier in the same session, its
# _ensure_cards_importable() stubs sys.modules["app.ai_orchestrator.orchestrator"]
# with a bare MagicMock (to avoid importing LangGraph). That stub is registered
# only in sys.modules, not as an attribute on the real app.ai_orchestrator
# package, so `from app.ai_orchestrator.orchestrator import AIOrchestrator` here
# would resolve to the stale stub and `patch("...orchestrator.X")` would then
# fail with AttributeError. Drop any stale stub so this file always imports the
# real orchestrator module.
sys.modules.pop("app.ai_orchestrator.orchestrator", None)

# ---------------------------------------------------------------------------
# Import-stub helpers for app.routers.books / app.routers.cards (Plan 02)
# Mirrors _ensure_books_importable() / _ensure_cards_importable() from
# test_ai_magic.py — app.auth.firebase_auth uses Python 3.10+ `dict | None`
# syntax which fails to import on the Python 3.9 test runner; quiz_ai.py
# (imported transitively by cards.py) needs slowapi + app.models.quiz stubs.
# ---------------------------------------------------------------------------


def _ensure_books_importable() -> None:
    if "google" not in sys.modules:
        sys.modules["google"] = MagicMock()

    mock_firebase = MagicMock()
    mock_firebase.get_firebase_user = MagicMock()
    sys.modules.setdefault("app.auth.firebase_auth", mock_firebase)

    mock_deps = MagicMock()
    mock_deps.require_ownership = MagicMock()
    mock_deps.track_ai_usage = MagicMock()
    mock_deps.get_subscription_tier = MagicMock()
    sys.modules.setdefault("app.auth.dependencies", mock_deps)


def _ensure_cards_importable() -> None:
    _ensure_books_importable()

    # cards.py uses `except GeminiQuotaError as exc:` / `except (GeminiTransientError, Exception)`
    # — these must be real BaseException subclasses, not MagicMock attributes, or Python raises
    # "TypeError: catching classes that do not inherit from BaseException". When test_ai_magic.py
    # runs earlier in the same session, its _ensure_cards_importable() already registered
    # sys.modules["app.ai_orchestrator.llm_clients.gemini_client"] as a bare MagicMock (whose
    # .GeminiQuotaError/.GeminiTransientError attributes are themselves MagicMocks) — setdefault
    # would be a no-op against that stale stub, so this is an unconditional overwrite. Combined
    # with the sys.modules.pop("app.routers.cards", ...) below, cards.py is re-imported fresh
    # against THESE real exception classes.
    mock_gemini_mod = MagicMock()

    class _GeminiQuotaError(RuntimeError):
        pass

    class _GeminiTransientError(RuntimeError):
        pass

    mock_gemini_mod.GeminiQuotaError = _GeminiQuotaError
    mock_gemini_mod.GeminiTransientError = _GeminiTransientError
    sys.modules["app.ai_orchestrator.llm_clients.gemini_client"] = mock_gemini_mod
    if "app.config.subscription_plans" not in sys.modules:
        sys.modules["app.config.subscription_plans"] = MagicMock()
    if "slowapi" not in sys.modules:
        sys.modules["slowapi"] = MagicMock()
    if "slowapi.util" not in sys.modules:
        sys.modules["slowapi.util"] = MagicMock()

    # quiz_ai.py applies @limiter.limit("5/minute") to start_ai_quiz_session. A bare
    # MagicMock's .limit(...) call returns a MagicMock, and applying THAT as a decorator
    # replaces the real async function with a MagicMock — `await quiz_ai_module.
    # start_ai_quiz_session(...)` then raises "TypeError: object MagicMock can't be used
    # in 'await' expression". test_ai_magic.py may have already registered
    # sys.modules["app.core.limiter"] as a bare MagicMock, so this is an unconditional
    # overwrite with a real passthrough decorator (mirrors the gemini_client fix above).
    mock_limiter_mod = MagicMock()
    mock_limiter_instance = MagicMock()
    mock_limiter_instance.limit = lambda *a, **k: (lambda fn: fn)
    mock_limiter_mod.limiter = mock_limiter_instance
    sys.modules["app.core.limiter"] = mock_limiter_mod

    if "app.models.quiz" not in sys.modules:
        from typing import Optional as _Optional

        from pydantic import BaseModel as _BM
        from pydantic import Field as _Field

        class _AIQuizQuestionResponse(_BM):
            card_id: str
            question_type: str
            question_text: str
            options: _Optional[list] = None
            hint_available: bool = True
            card_index: int

        class _AIQuizQuestionStored(_BM):
            card_id: str
            question_type: str
            question_text: str
            options: _Optional[list] = None
            hint_available: bool = True
            correct_answer: str
            rubric: str = ""
            card_index: int

        class _AIQuizStartRequest(_BM):
            topic: _Optional[str] = _Field(default=None, max_length=200)
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


def _ensure_tts_importable() -> None:
    """tts.py and its tts_client helper do `from google.cloud import texttospeech` /
    `from google.oauth2 import service_account`. _ensure_books_importable() (above)
    registers sys.modules["google"] as a bare MagicMock() when "google" is not
    already present — a MagicMock is not a package, so `from google.cloud import
    texttospeech` then raises "ModuleNotFoundError: No module named 'google.cloud';
    'google' is not a package". Register the submodules tts.py needs directly in
    sys.modules so the `from X.Y import Z` form resolves to MagicMock attributes."""
    for mod_name in (
        "google.cloud",
        "google.cloud.texttospeech",
        "google.oauth2",
        "google.oauth2.service_account",
    ):
        sys.modules.setdefault(mod_name, MagicMock())

    if "app.ai_orchestrator.llm_clients.tts_client" not in sys.modules:
        sys.modules["app.ai_orchestrator.llm_clients.tts_client"] = MagicMock()


_ensure_cards_importable()
_ensure_tts_importable()


# Drop any stale app.routers.books / app.routers.cards stubs left by
# test_ai_magic.py-style helpers in the same session — this file needs the
# REAL router modules (to exercise ai_expand_text / generate_cards_from_book
# directly), not MagicMock stand-ins.
sys.modules.pop("app.routers.books", None)
sys.modules.pop("app.routers.cards", None)
sys.modules.pop("app.routers.quiz_ai", None)
sys.modules.pop("app.routers.tts", None)

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures (reused by Plans 02-05)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_langfuse_client():
    """MagicMock Langfuse client with context-manager-compatible
    start_as_current_observation / start_as_current_generation that record
    calls for assertion (propagate_attributes kwargs, nesting, etc.)."""
    client = MagicMock()
    client.start_as_current_observation.return_value.__enter__.return_value = MagicMock()
    client.start_as_current_generation.return_value.__enter__.return_value = MagicMock()
    return client


@pytest.fixture
def broken_langfuse_client():
    """Simulates an unreachable Langfuse — every SDK method raises."""
    client = MagicMock()
    client.start_as_current_observation.side_effect = ConnectionError("Langfuse unreachable")
    client.start_as_current_generation.side_effect = ConnectionError("Langfuse unreachable")
    return client


# ---------------------------------------------------------------------------
# Scenario 1 & 2 — orchestrator.invoke() (Pattern A, D-02, TR-01)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "graph_name,expected_feature",
    [("rag", "cards_magic"), ("quiz", "quiz_magic"), ("visualizer", "viz_magic")],
)
def test_orchestrator_happy_path_attaches_callback_handler(
    graph_name, expected_feature, mock_langfuse_client
):
    """Scenario 1: healthy Langfuse client -> CallbackHandler attached,
    propagate_attributes carries user_id/tier/feature per D-08 pipeline mapping."""
    from app.ai_orchestrator.orchestrator import AIOrchestrator

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = {"generated_cards": []}

    orch = AIOrchestrator()
    orch.graphs = {graph_name: fake_graph}

    state = {"tier": "pro", "user_id": "u1", "prompt": "test"}

    with patch(
        "app.ai_orchestrator.orchestrator.get_langfuse_client",
        return_value=mock_langfuse_client,
    ), patch("app.ai_orchestrator.orchestrator.CallbackHandler") as MockHandler, patch(
        "app.ai_orchestrator.orchestrator.propagate_attributes"
    ) as mock_propagate:
        mock_propagate.return_value.__enter__.return_value = None
        mock_propagate.return_value.__exit__.return_value = None

        result = orch.invoke(graph_name, state)

    assert result == {"generated_cards": []}
    MockHandler.assert_called_once()
    fake_graph.invoke.assert_called_once()
    call_args, call_kwargs = fake_graph.invoke.call_args
    assert "config" in call_kwargs
    assert "callbacks" in call_kwargs["config"]

    mock_propagate.assert_called_once()
    _, propagate_kwargs = mock_propagate.call_args
    assert propagate_kwargs["user_id"] == "u1"
    assert propagate_kwargs["trace_name"] == expected_feature
    assert propagate_kwargs["metadata"]["feature"] == expected_feature
    assert propagate_kwargs["metadata"]["tier"] == "pro"
    assert propagate_kwargs["metadata"]["user_id"] == "u1"
    assert "model" in propagate_kwargs["metadata"]
    assert propagate_kwargs["tags"] == [expected_feature, "pro"]


def test_orchestrator_langfuse_unreachable_falls_back(mock_langfuse_client, caplog):
    """Scenario 2: Langfuse unreachable -> graph.invoke(state) runs unwrapped (no config kwarg),
    identical result, exactly 1 WARNING log."""
    import logging
    from app.ai_orchestrator.orchestrator import AIOrchestrator

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = {"generated_cards": ["card1"]}

    orch = AIOrchestrator()
    orch.graphs = {"rag": fake_graph}

    state = {"tier": "free", "user_id": "u2", "prompt": "test"}

    with patch(
        "app.ai_orchestrator.orchestrator.get_langfuse_client",
        return_value=mock_langfuse_client,
    ), patch(
        "app.ai_orchestrator.orchestrator.CallbackHandler",
        side_effect=ConnectionError("Langfuse unreachable"),
    ), patch("app.ai_orchestrator.orchestrator.propagate_attributes") as mock_propagate:
        mock_propagate.return_value.__enter__.return_value = None
        mock_propagate.return_value.__exit__.return_value = None

        with caplog.at_level(logging.WARNING):
            result = orch.invoke("rag", state)

    assert result == {"generated_cards": ["card1"]}
    # graph.invoke called exactly twice is WRONG — must be called exactly ONCE,
    # the fallback untraced call (config kwarg NOT passed)
    fake_graph.invoke.assert_called_once()
    call_args, call_kwargs = fake_graph.invoke.call_args
    assert "config" not in call_kwargs

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert "Langfuse tracing failed" in warning_records[0].message


def test_orchestrator_no_client_skips_tracing_entirely():
    """client=None baseline — graph.invoke(state) called with no config kwarg, no Langfuse calls."""
    from app.ai_orchestrator.orchestrator import AIOrchestrator

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = {"generated_cards": []}

    orch = AIOrchestrator()
    orch.graphs = {"rag": fake_graph}

    state = {"tier": "free", "user_id": "u3", "prompt": "test"}

    with patch("app.ai_orchestrator.orchestrator.get_langfuse_client", return_value=None):
        result = orch.invoke("rag", state)

    assert result == {"generated_cards": []}
    fake_graph.invoke.assert_called_once_with(state)


# ---------------------------------------------------------------------------
# Scenarios 3 & 4 — books.py ai_expand_text (Pattern B, D-03, feature=book_expand, TR-02)
# ---------------------------------------------------------------------------

import logging


def _fake_groq_completion(text: str, prompt_tokens=100, completion_tokens=50, total_tokens=150):
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=text))]
    completion.usage = MagicMock(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens
    )
    return completion


@pytest.mark.asyncio
async def test_ai_expand_text_happy_path_traces_book_expand(mock_langfuse_client):
    from app.models.ai_expand import AIExpandRequest
    import app.routers.books as books_module

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None}
    fake_groq_client = MagicMock()
    fake_groq_client.chat.completions.create.return_value = _fake_groq_completion("Expanded text here.")

    body = AIExpandRequest(instruction="Expand this", selected_text="short text")
    current_user = {"user_id": "u1", "subscription": {"tier": "free"}}

    with patch.object(books_module, "books_collection") as mock_books_collection, \
         patch.object(books_module, "get_client_for_tier", return_value=fake_groq_client), \
         patch.object(books_module, "get_langfuse_client", return_value=mock_langfuse_client), \
         patch.object(books_module, "ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        response = await books_module.ai_expand_text(
            book_id="abc123", body=body, current_user=current_user
        )

    assert response.expanded_text == "Expanded text here."

    mock_langfuse_client.start_as_current_generation.assert_called_once()
    _, gen_kwargs = mock_langfuse_client.start_as_current_generation.call_args
    assert gen_kwargs["name"] == "book_expand"

    generation_ctx = mock_langfuse_client.start_as_current_generation.return_value.__enter__.return_value
    generation_ctx.update.assert_called_once()
    _, update_kwargs = generation_ctx.update.call_args
    assert update_kwargs["output"] == "Expanded text here."
    assert update_kwargs["usage_details"] is not None
    assert update_kwargs["usage_details"]["total"] == 150


@pytest.mark.asyncio
async def test_ai_expand_text_langfuse_unreachable(broken_langfuse_client, caplog):
    from app.models.ai_expand import AIExpandRequest
    import app.routers.books as books_module

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None}
    fake_groq_client = MagicMock()
    fake_groq_client.chat.completions.create.return_value = _fake_groq_completion("Expanded text here.")

    body = AIExpandRequest(instruction="Expand this", selected_text="short text")
    current_user = {"user_id": "u1", "subscription": {"tier": "free"}}

    with patch.object(books_module, "books_collection") as mock_books_collection, \
         patch.object(books_module, "get_client_for_tier", return_value=fake_groq_client), \
         patch.object(books_module, "get_langfuse_client", return_value=broken_langfuse_client), \
         patch.object(books_module, "ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        with caplog.at_level(logging.WARNING):
            response = await books_module.ai_expand_text(
                book_id="abc123", body=body, current_user=current_user
            )

    assert response.expanded_text == "Expanded text here."
    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Langfuse tracing failed" in r.message
    ]
    assert len(warning_records) == 1


# ---------------------------------------------------------------------------
# cards.py generate_cards_from_book (Pattern B, feature=book_cards, correction #1, TR-02)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cards_from_book_happy_path_traces_book_cards(mock_langfuse_client):
    from app.models.book_generation import GenerateFromBookRequest
    import app.routers.cards as cards_module

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None, "full_content": "Some book text"}
    fake_gemini_client = MagicMock()
    fake_gemini_completion = MagicMock()
    fake_gemini_completion.choices = [
        MagicMock(message=MagicMock(content='[{"title": "Q1", "content": "A1"}]'))
    ]
    fake_gemini_client.request.return_value = fake_gemini_completion

    body = GenerateFromBookRequest(book_id="abc123")
    current_user = {"user_id": "u1"}

    with patch.object(cards_module, "books_collection") as mock_books_collection, \
         patch.object(cards_module, "get_client_for_tier", return_value=fake_gemini_client), \
         patch.object(cards_module, "get_langfuse_client", return_value=mock_langfuse_client), \
         patch("bson.ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        response = await cards_module.generate_cards_from_book(
            body=body, current_user=current_user, tier="plus"
        )

    assert len(response.cards) == 1
    assert response.cards[0].title == "Q1"

    mock_langfuse_client.start_as_current_generation.assert_called_once()
    _, gen_kwargs = mock_langfuse_client.start_as_current_generation.call_args
    assert gen_kwargs["name"] == "book_cards"

    generation_ctx = mock_langfuse_client.start_as_current_generation.return_value.__enter__.return_value
    generation_ctx.update.assert_called_once()
    _, update_kwargs = generation_ctx.update.call_args
    assert update_kwargs["usage_details"] is None


@pytest.mark.asyncio
async def test_generate_cards_from_book_langfuse_unreachable(broken_langfuse_client, caplog):
    from app.models.book_generation import GenerateFromBookRequest
    import app.routers.cards as cards_module

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None, "full_content": "Some book text"}
    fake_gemini_client = MagicMock()
    fake_gemini_completion = MagicMock()
    fake_gemini_completion.choices = [
        MagicMock(message=MagicMock(content='[{"title": "Q1", "content": "A1"}]'))
    ]
    fake_gemini_client.request.return_value = fake_gemini_completion

    body = GenerateFromBookRequest(book_id="abc123")
    current_user = {"user_id": "u1"}

    with patch.object(cards_module, "books_collection") as mock_books_collection, \
         patch.object(cards_module, "get_client_for_tier", return_value=fake_gemini_client), \
         patch.object(cards_module, "get_langfuse_client", return_value=broken_langfuse_client), \
         patch("bson.ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        with caplog.at_level(logging.WARNING):
            response = await cards_module.generate_cards_from_book(
                body=body, current_user=current_user, tier="plus"
            )

    assert len(response.cards) == 1
    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Langfuse tracing failed" in r.message
    ]
    assert len(warning_records) == 1


# ---------------------------------------------------------------------------
# Scenarios 5 & 6 — quiz_ai.py _generate_questions (Pattern B, D-04/D-09, feature=quiz_from_deck, TR-03)
# ---------------------------------------------------------------------------


def _fake_quiz_json(n=1):
    items = [
        {
            "question_type": "short_answer",
            "question_text": f"Q{i}",
            "correct_answer": f"A{i}",
            "rubric": "exact match",
        }
        for i in range(n)
    ]
    return json.dumps(items)


@pytest.mark.asyncio
async def test_generate_questions_retries_traced_separately(mock_langfuse_client):
    import app.routers.quiz_ai as quiz_ai_module

    # Attempt 1 returns EMPTY content (not an exception) — this lets attempt 1's
    # start_as_current_generation span complete normally (generation.update is called
    # with output=""), the "[ai_quiz] LLM returned empty content on attempt 1...
    # Retrying" branch fires, and the loop proceeds to attempt 2, which opens its OWN
    # span and succeeds. This is Known Failure Mode 1: both attempts are visible as
    # separate Langfuse traces, not just the winner. (An exception on attempt 1 would
    # instead be caught by the inner "Langfuse tracing failed" except — misattributing
    # a genuine LLM error — and its same-attempt fallback retry would consume the
    # second side_effect item, producing only ONE start_as_current_generation call.)
    fake_groq_client = MagicMock()
    fake_groq_client.chat.completions.create.side_effect = [
        _fake_groq_completion(""),
        _fake_groq_completion(_fake_quiz_json(1)),
    ]

    with patch.object(quiz_ai_module, "get_client_for_tier", return_value=fake_groq_client), \
         patch.object(quiz_ai_module, "get_langfuse_client", return_value=mock_langfuse_client):
        questions = await quiz_ai_module._generate_questions(
            topic="Photosynthesis",
            question_count=1,
            language="en",
            tier="free",
            user_id="u1",
            feature="quiz_from_deck",
        )

    assert len(questions) == 1
    assert questions[0].question_text == "Q0"

    # Both attempts must produce their own start_as_current_generation span (Known Failure Mode 1)
    assert mock_langfuse_client.start_as_current_generation.call_count == 2
    for call in mock_langfuse_client.start_as_current_generation.call_args_list:
        _, kwargs = call
        assert kwargs["name"] == "quiz_from_deck"


@pytest.mark.asyncio
async def test_generate_questions_langfuse_unreachable(broken_langfuse_client, caplog):
    import app.routers.quiz_ai as quiz_ai_module

    fake_groq_client = MagicMock()
    fake_groq_client.chat.completions.create.return_value = _fake_groq_completion(_fake_quiz_json(1))

    with patch.object(quiz_ai_module, "get_client_for_tier", return_value=fake_groq_client), \
         patch.object(quiz_ai_module, "get_langfuse_client", return_value=broken_langfuse_client):
        with caplog.at_level(logging.WARNING):
            questions = await quiz_ai_module._generate_questions(
                topic="Photosynthesis",
                question_count=1,
                language="en",
                tier="free",
                user_id="u1",
                feature="quiz_from_deck",
            )

    assert len(questions) == 1
    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Langfuse tracing failed on attempt" in r.message
    ]
    assert len(warning_records) >= 1


@pytest.mark.asyncio
async def test_start_ai_quiz_session_passes_quiz_from_deck_feature():
    import app.routers.quiz_ai as quiz_ai_module
    from app.models.quiz import AIQuizQuestionStored, AIQuizStartRequest

    fake_question = AIQuizQuestionStored(
        card_id="c1", question_type="short_answer", question_text="Q1",
        options=None, hint_available=True, correct_answer="A1", rubric="exact", card_index=0,
    )

    fake_user_doc = {"subscription": {"tier": "free"}, "preferences": {}}
    body = AIQuizStartRequest(topic="Photosynthesis", question_count=5, language="en")
    current_user = {"user_id": "u1"}

    with patch.object(quiz_ai_module, "users_collection") as mock_users_collection, \
         patch.object(quiz_ai_module, "ai_quiz_sessions_collection") as mock_sessions_collection, \
         patch.object(quiz_ai_module, "_generate_questions", new_callable=AsyncMock) as mock_generate, \
         patch.object(quiz_ai_module, "ObjectId", side_effect=lambda x: x):
        mock_users_collection.find_one = AsyncMock(return_value=fake_user_doc)
        mock_sessions_collection.insert_one = AsyncMock(return_value=None)
        mock_generate.return_value = [fake_question]

        from starlette.requests import Request as StarletteRequest
        fake_request = MagicMock(spec=StarletteRequest)

        await quiz_ai_module.start_ai_quiz_session(
            request=fake_request, body=body, current_user=current_user
        )

    mock_generate.assert_called_once()
    _, call_kwargs = mock_generate.call_args
    assert call_kwargs["feature"] == "quiz_from_deck"
    assert call_kwargs["user_id"] == "u1"


# ---------------------------------------------------------------------------
# quiz_ai.py generate_quiz_from_book (Pattern B, feature=quiz_from_book, correction #2, TR-03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_quiz_from_book_happy_path_traces_quiz_from_book(mock_langfuse_client):
    import app.routers.quiz_ai as quiz_ai_module
    from app.models.book_generation import GenerateQuizFromBookRequest

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None, "full_content": "Some book text"}
    fake_gemini_client = MagicMock()
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock(message=MagicMock(content=_fake_quiz_json(2)))]
    fake_gemini_client.request.return_value = fake_completion

    body = GenerateQuizFromBookRequest(book_id="abc123")
    current_user = {"user_id": "u1"}

    with patch.object(quiz_ai_module, "books_collection") as mock_books_collection, \
         patch.object(quiz_ai_module, "get_client_for_tier", return_value=fake_gemini_client), \
         patch.object(quiz_ai_module, "get_langfuse_client", return_value=mock_langfuse_client), \
         patch.object(quiz_ai_module, "ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        result = await quiz_ai_module.generate_quiz_from_book(
            body=body, current_user=current_user, tier="plus"
        )

    assert len(result["questions"]) == 2

    mock_langfuse_client.start_as_current_generation.assert_called_once()
    _, gen_kwargs = mock_langfuse_client.start_as_current_generation.call_args
    assert gen_kwargs["name"] == "quiz_from_book"

    generation_ctx = mock_langfuse_client.start_as_current_generation.return_value.__enter__.return_value
    generation_ctx.update.assert_called_once()
    _, update_kwargs = generation_ctx.update.call_args
    assert update_kwargs["usage_details"] is None


@pytest.mark.asyncio
async def test_generate_quiz_from_book_langfuse_unreachable(broken_langfuse_client, caplog):
    import app.routers.quiz_ai as quiz_ai_module
    from app.models.book_generation import GenerateQuizFromBookRequest

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None, "full_content": "Some book text"}
    fake_gemini_client = MagicMock()
    fake_completion = MagicMock()
    fake_completion.choices = [MagicMock(message=MagicMock(content=_fake_quiz_json(2)))]
    fake_gemini_client.request.return_value = fake_completion

    body = GenerateQuizFromBookRequest(book_id="abc123")
    current_user = {"user_id": "u1"}

    with patch.object(quiz_ai_module, "books_collection") as mock_books_collection, \
         patch.object(quiz_ai_module, "get_client_for_tier", return_value=fake_gemini_client), \
         patch.object(quiz_ai_module, "get_langfuse_client", return_value=broken_langfuse_client), \
         patch.object(quiz_ai_module, "ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        with caplog.at_level(logging.WARNING):
            result = await quiz_ai_module.generate_quiz_from_book(
                body=body, current_user=current_user, tier="plus"
            )

    assert len(result["questions"]) == 2
    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Langfuse tracing failed" in r.message
    ]
    assert len(warning_records) == 1


# ---------------------------------------------------------------------------
# Scenarios 7 & 8 — agent.py chat() (Pattern C, D-05/D-10/D-11, feature=smart_pet_chat, TR-04)
# ---------------------------------------------------------------------------


def _ensure_agent_importable() -> None:
    """app.routers.agent transitively imports groq, google.generativeai (+ subpackages),
    and app.models.quiz (which uses `str | None` syntax — handled by eval-type-backport
    + from __future__ import annotations in agent.py itself). The shared
    _ensure_cards_importable() stubs cover slowapi/limiter/firebase_auth/dependencies;
    this adds the google.generativeai subpackage stubs (test_smart_pet.py precedent)
    and a real-Pydantic app.models.quiz.QuizConfig (the existing mock_quiz_mod stub
    from _ensure_cards_importable() does not define QuizConfig, which agent.py needs)."""
    for mod in ("google.generativeai", "google.generativeai.types",
                "google.generativeai.protos", "google.api_core.exceptions"):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    # app.models.quiz may already be registered (as a bare MagicMock) by
    # _ensure_cards_importable() — a MagicMock's .QuizConfig attribute is itself
    # a MagicMock (hasattr is always True), which breaks Pydantic's `QuizConfig | None`
    # schema generation in agent.py's ChatResponse. Unconditionally overwrite
    # .QuizConfig with a real Pydantic model (same unconditional-overwrite pattern
    # as the gemini_client/limiter stubs above).
    from typing import Optional as _Optional
    from pydantic import BaseModel as _BM

    class _QuizConfig(_BM):
        mode: str
        topic: _Optional[str] = None
        question_count: int = 10
        deck_id: _Optional[str] = None

    quiz_mod = sys.modules.get("app.models.quiz")
    if quiz_mod is None:
        quiz_mod = MagicMock()
        sys.modules["app.models.quiz"] = quiz_mod
    quiz_mod.QuizConfig = _QuizConfig

    # _ensure_cards_importable() registers app.config.subscription_plans as a bare
    # MagicMock() (since cards.py only needs it importable, not functionally correct).
    # agent.py's chat() relies on REAL SUBSCRIPTION_PLANS/SubscriptionTier values —
    # `SUBSCRIPTION_PLANS[tier]["features"].get("agent_persistent_memory")` must
    # evaluate to a real bool (False for free tier) to correctly gate
    # _append_to_persistent_history(). A MagicMock subscript/`.get()` chain returns
    # a truthy MagicMock regardless of tier, which would call that function
    # unconditionally. Unconditionally overwrite with the real module (same
    # unconditional-overwrite pattern as gemini_client/limiter/QuizConfig above) —
    # the real module has no problematic transitive imports, so this is safe.
    import importlib

    sys.modules.pop("app.config.subscription_plans", None)
    sys.modules["app.config.subscription_plans"] = importlib.import_module(
        "app.config.subscription_plans"
    )

    # When the full test suite runs, tests/test_blackboards.py (collected before
    # test_tracing.py alphabetically) registers app.models.agent_models as a bare
    # MagicMock(). agent.py's /generate-personality route declares
    # `response_model=GeneratePersonalityResponse` — FastAPI rejects a MagicMock as
    # an invalid Pydantic field type at router-registration time
    # (fastapi.exceptions.FastAPIError: "Invalid args for response field!").
    # Unconditionally overwrite with the real module (same pattern as
    # subscription_plans above); it has no problematic transitive imports.
    sys.modules.pop("app.models.agent_models", None)
    sys.modules["app.models.agent_models"] = importlib.import_module(
        "app.models.agent_models"
    )


_ensure_agent_importable()
sys.modules.pop("app.routers.agent", None)


def _make_chat_request_body(message="Hello pet"):
    from app.routers.agent import ChatRequest
    return ChatRequest(message=message, history=[], language="en", context=None)


@pytest.mark.asyncio
async def test_chat_happy_path_traces_smart_pet_chat_with_session_id(mock_langfuse_client):
    import app.routers.agent as agent_module

    fake_user = {
        "_id": "u1",
        "subscription": {"tier": "free"},
        "agent": {"xp": 0},
        "preferences": {},
    }

    with patch.object(agent_module, "users_collection") as mock_users, \
         patch.object(agent_module, "get_langfuse_client", return_value=mock_langfuse_client), \
         patch.object(agent_module, "_detect_quiz_intent", return_value=None), \
         patch.object(agent_module, "_append_to_persistent_history", new_callable=AsyncMock) as mock_append, \
         patch.object(agent_module, "_load_persistent_history", new_callable=AsyncMock, return_value=[]), \
         patch.object(agent_module.agent_llm, "chat", new_callable=AsyncMock) as mock_agent_chat, \
         patch.object(agent_module, "grant_xp", new_callable=AsyncMock,
                       return_value={"level_up": False, "new_level": 1, "new_stage": 1}), \
         patch.object(agent_module, "ObjectId", side_effect=lambda x: x):
        mock_users.find_one = AsyncMock(return_value=fake_user)
        mock_users.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        mock_agent_chat.return_value = "Hello! How can I help you study today?"

        body = _make_chat_request_body()
        fake_request = MagicMock()

        response = await agent_module.chat(
            request=fake_request, body=body, current_user={"user_id": "u1"}
        )

    assert response.reply == "Hello! How can I help you study today?"
    assert mock_agent_chat.call_count == 1  # TR-06: single LLM invocation

    mock_langfuse_client.start_as_current_generation.assert_called_once()
    _, gen_kwargs = mock_langfuse_client.start_as_current_generation.call_args
    assert gen_kwargs["name"] == "smart_pet_chat"

    generation_ctx = mock_langfuse_client.start_as_current_generation.return_value.__enter__.return_value
    generation_ctx.update.assert_called_once_with(output="Hello! How can I help you study today?")

    mock_append.assert_not_called()  # free tier — agent_persistent_memory feature is False


@pytest.mark.asyncio
async def test_chat_session_id_stable_across_two_turns(mock_langfuse_client):
    import app.routers.agent as agent_module

    fake_user = {
        "_id": "u1",
        "subscription": {"tier": "free"},
        "agent": {"xp": 0},
        "preferences": {},
    }

    propagate_calls = []

    def _capture_propagate(*args, **kwargs):
        propagate_calls.append(kwargs)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=None)
        cm.__exit__ = MagicMock(return_value=None)
        return cm

    with patch.object(agent_module, "users_collection") as mock_users, \
         patch.object(agent_module, "get_langfuse_client", return_value=mock_langfuse_client), \
         patch.object(agent_module, "propagate_attributes", side_effect=_capture_propagate), \
         patch.object(agent_module, "_detect_quiz_intent", return_value=None), \
         patch.object(agent_module, "_append_to_persistent_history", new_callable=AsyncMock), \
         patch.object(agent_module, "_load_persistent_history", new_callable=AsyncMock, return_value=[]), \
         patch.object(agent_module.agent_llm, "chat", new_callable=AsyncMock, return_value="Reply"), \
         patch.object(agent_module, "grant_xp", new_callable=AsyncMock,
                       return_value={"level_up": False, "new_level": 1, "new_stage": 1}), \
         patch.object(agent_module, "ObjectId", side_effect=lambda x: x):
        mock_users.find_one = AsyncMock(return_value=fake_user)
        mock_users.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        fake_request = MagicMock()

        await agent_module.chat(request=fake_request, body=_make_chat_request_body("Turn 1"), current_user={"user_id": "u1"})
        await agent_module.chat(request=fake_request, body=_make_chat_request_body("Turn 2"), current_user={"user_id": "u1"})

    assert len(propagate_calls) == 2
    assert propagate_calls[0]["session_id"] == "u1"
    assert propagate_calls[1]["session_id"] == "u1"


@pytest.mark.asyncio
async def test_dispatch_tool_call_nested_span(mock_langfuse_client):
    import app.routers.agent as agent_module

    with patch.object(agent_module, "list_decks", new_callable=AsyncMock, return_value=[{"id": "d1", "name": "Deck 1"}]):
        result_str = await agent_module._dispatch_tool_call("list_decks", {}, "u1", client=mock_langfuse_client)

    mock_langfuse_client.start_as_current_observation.assert_called_once()
    _, obs_kwargs = mock_langfuse_client.start_as_current_observation.call_args
    assert obs_kwargs["name"] == "tool:list_decks"
    assert obs_kwargs["as_type"] == "tool"
    assert obs_kwargs["input"] == {}

    tool_span_ctx = mock_langfuse_client.start_as_current_observation.return_value.__enter__.return_value
    tool_span_ctx.update.assert_called_once()
    _, update_kwargs = tool_span_ctx.update.call_args
    assert update_kwargs["output"] == result_str
    assert "Deck 1" in result_str


@pytest.mark.asyncio
async def test_chat_langfuse_unreachable(broken_langfuse_client, caplog):
    import app.routers.agent as agent_module

    fake_user = {
        "_id": "u1",
        "subscription": {"tier": "free"},
        "agent": {"xp": 0},
        "preferences": {},
    }

    with patch.object(agent_module, "users_collection") as mock_users, \
         patch.object(agent_module, "get_langfuse_client", return_value=broken_langfuse_client), \
         patch.object(agent_module, "_detect_quiz_intent", return_value=None), \
         patch.object(agent_module, "_append_to_persistent_history", new_callable=AsyncMock) as mock_append, \
         patch.object(agent_module, "_load_persistent_history", new_callable=AsyncMock, return_value=[]), \
         patch.object(agent_module.agent_llm, "chat", new_callable=AsyncMock, return_value="Hello there!") as mock_agent_chat, \
         patch.object(agent_module, "grant_xp", new_callable=AsyncMock,
                       return_value={"level_up": False, "new_level": 1, "new_stage": 1}), \
         patch.object(agent_module, "ObjectId", side_effect=lambda x: x):
        mock_users.find_one = AsyncMock(return_value=fake_user)
        mock_users.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        fake_request = MagicMock()

        with caplog.at_level(logging.WARNING):
            response = await agent_module.chat(
                request=fake_request, body=_make_chat_request_body(), current_user={"user_id": "u1"}
            )

    assert response.reply == "Hello there!"
    assert mock_agent_chat.call_count == 1  # TR-06: single LLM invocation even when Langfuse fails
    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Langfuse tracing failed" in r.message
    ]
    assert len(warning_records) == 1


# ---------------------------------------------------------------------------
# Scenarios 9 & 10 — tts.py generate_tts (Pattern D, D-01/D-06/D-12, feature=tts_amagic, TR-04)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_tts_happy_path_traces_tts_amagic(mock_langfuse_client):
    import app.routers.tts as tts_module
    from app.models.tts import TTSRequest

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None}
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"fake-mp3-bytes")

    body = TTSRequest(text="Hello world", language_code="fr-FR")
    current_user = {"user_id": "u1"}

    with patch.object(tts_module, "books_collection") as mock_books_collection, \
         patch.object(tts_module, "get_tts_client", return_value=fake_tts_client), \
         patch.object(tts_module, "get_langfuse_client", return_value=mock_langfuse_client), \
         patch.object(tts_module, "ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        response = await tts_module.generate_tts(
            book_id="abc123", body=body, tier="pro", current_user=current_user
        )

    assert response.body == b"fake-mp3-bytes"
    assert fake_tts_client.synthesize_speech.call_count == 1  # TR-06: single synthesis call

    mock_langfuse_client.start_as_current_observation.assert_called_once()
    _, obs_kwargs = mock_langfuse_client.start_as_current_observation.call_args
    assert obs_kwargs["name"] == "tts_amagic"
    assert obs_kwargs["as_type"] == "span"
    assert obs_kwargs["input"]["language_code"] == "fr-FR"

    span_ctx = mock_langfuse_client.start_as_current_observation.return_value.__enter__.return_value
    span_ctx.update.assert_called_once()
    _, update_kwargs = span_ctx.update.call_args
    output = update_kwargs["output"]
    assert "audio_byte_size" in output
    assert "voice_ssml_gender" in output
    assert "latency_ms" in output
    assert "audio_content" not in output
    assert update_kwargs["metadata"]["voice_name"] == "fr-FR"


@pytest.mark.asyncio
async def test_generate_tts_langfuse_unreachable(broken_langfuse_client, caplog):
    import app.routers.tts as tts_module
    from app.models.tts import TTSRequest

    fake_book = {"_id": "abc123", "user_id": "u1", "deleted_at": None}
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"fake-mp3-bytes")

    body = TTSRequest(text="Hello world", language_code="fr-FR")
    current_user = {"user_id": "u1"}

    with patch.object(tts_module, "books_collection") as mock_books_collection, \
         patch.object(tts_module, "get_tts_client", return_value=fake_tts_client), \
         patch.object(tts_module, "get_langfuse_client", return_value=broken_langfuse_client), \
         patch.object(tts_module, "ObjectId", side_effect=lambda x: x):
        mock_books_collection.find_one = AsyncMock(return_value=fake_book)

        with caplog.at_level(logging.WARNING):
            response = await tts_module.generate_tts(
                book_id="abc123", body=body, tier="pro", current_user=current_user
            )

    assert response.body == b"fake-mp3-bytes"
    assert fake_tts_client.synthesize_speech.call_count == 1  # TR-06: single synthesis call even when Langfuse fails
    warning_records = [
        r for r in caplog.records
        if r.levelname == "WARNING" and "Langfuse tracing failed" in r.message
    ]
    assert len(warning_records) == 1


# ---------------------------------------------------------------------------
# CI-blocking guardrails (Section 6) — fail-open (TR-06), metadata/taxonomy-shape (TR-05),
# no-@observe-on-TTS (D-06/SC6)
# ---------------------------------------------------------------------------


def test_no_observe_decorator_on_tts():
    """Guardrail 3 (D-06/SC6): tts.py must never use @observe or any langfuse decorator —
    manual as_type="span" only."""
    import re
    from pathlib import Path

    tts_source = Path(__file__).parent.parent / "app" / "routers" / "tts.py"
    content = tts_source.read_text()

    forbidden_patterns = [
        r"@observe",
        r"@langfuse\.observe",
        r"from langfuse import observe",
        r"from langfuse\.decorators import observe",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, content), f"Forbidden pattern '{pattern}' found in tts.py"

    assert 'as_type="generation"' not in content
    assert 'as_type="span"' in content


# D-08 taxonomy — the 9 canonical feature/trace_name values, one per instrumented call path
_D08_FEATURE_TAXONOMY = [
    "cards_magic",
    "quiz_magic",
    "viz_magic",
    "book_expand",
    "book_cards",
    "quiz_from_deck",
    "quiz_from_book",
    "smart_pet_chat",
    "tts_amagic",
]


def test_d08_taxonomy_has_nine_unique_features():
    """Guardrail 2a (TR-05): the D-08 taxonomy has exactly 9 unique feature strings."""
    assert len(_D08_FEATURE_TAXONOMY) == 9
    assert len(set(_D08_FEATURE_TAXONOMY)) == 9


@pytest.mark.parametrize(
    "file_path,expected_features",
    [
        ("app/ai_orchestrator/orchestrator.py", ["cards_magic", "quiz_magic", "viz_magic"]),
        ("app/routers/books.py", ["book_expand"]),
        ("app/routers/cards.py", ["book_cards"]),
        ("app/routers/quiz_ai.py", ["quiz_from_deck", "quiz_from_book"]),
        ("app/routers/agent.py", ["smart_pet_chat"]),
        ("app/routers/tts.py", ["tts_amagic"]),
    ],
)
def test_all_call_sites_use_d08_taxonomy_features(file_path, expected_features):
    """Guardrail 2b (TR-05): every instrumented file's trace_name/feature literals are drawn
    from the 9-value D-08 taxonomy — no renamed/drifted feature strings. This test is
    parametrized over 6 cases (one per instrumented file) and collects as 6 separate
    pytest items."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / file_path).read_text()
    for feature in expected_features:
        assert feature in _D08_FEATURE_TAXONOMY, f"{feature!r} is not in the D-08 taxonomy"
        assert f'"{feature}"' in source, f"{feature!r} not found as a string literal in {file_path}"


def test_fail_open_coverage_across_all_call_sites():
    """Guardrail 1 (TR-06): a fail-open ('Langfuse unreachable') test exists for every
    call-site family — orchestrator, book_expand, book_cards, quiz_from_deck, quiz_from_book,
    smart_pet_chat, tts_amagic. Meta-test guards against future edits silently dropping
    fail-open coverage."""
    import sys

    this_module = sys.modules[__name__]
    required_test_names = [
        "test_orchestrator_langfuse_unreachable_falls_back",
        "test_ai_expand_text_langfuse_unreachable",
        "test_generate_cards_from_book_langfuse_unreachable",
        "test_generate_questions_langfuse_unreachable",
        "test_generate_quiz_from_book_langfuse_unreachable",
        "test_chat_langfuse_unreachable",
        "test_generate_tts_langfuse_unreachable",
    ]
    for name in required_test_names:
        assert hasattr(this_module, name), f"Missing fail-open test: {name}"
        assert callable(getattr(this_module, name))
