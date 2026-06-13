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


_ensure_cards_importable()


# Drop any stale app.routers.books / app.routers.cards stubs left by
# test_ai_magic.py-style helpers in the same session — this file needs the
# REAL router modules (to exercise ai_expand_text / generate_cards_from_book
# directly), not MagicMock stand-ins.
sys.modules.pop("app.routers.books", None)
sys.modules.pop("app.routers.cards", None)
sys.modules.pop("app.routers.quiz_ai", None)

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
