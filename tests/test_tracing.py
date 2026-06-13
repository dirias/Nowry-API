"""
Phase 12 — LLM Tracing tests (TR-01..TR-06).

Covers the AI-SPEC Section 5 reference dataset (10 scenarios across 5 call sites x
2 conditions: Langfuse healthy vs. unreachable). Scenarios are added incrementally
as each call site is instrumented (12-01 through 12-05 plans).

Test isolation strategy mirrors test_langfuse_client.py / test_sync_langfuse.py:
sys.modules stubs prevent SDK import errors on the Python 3.9 test runner.
"""
from __future__ import annotations

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
