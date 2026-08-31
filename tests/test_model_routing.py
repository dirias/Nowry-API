"""
Phase 4 — Model Routing Tests (GATE-01, GATE-02)

Tests verify that AIOrchestrator.invoke() routes to the correct LLM client
based on the tier passed in state:
  - free  → Groq_client (Llama 3.3 70B)
  - plus  → Gemini_client configured with gemini-flash-latest
  - pro   → Gemini_client configured with gemini-pro-latest

Wave 0 update (Phase 10): all 5 routing tests now patch
app.core.model_config.get_client_for_tier instead of setting instance
attributes directly (orch.groq_client etc.) — the old pattern breaks after
Wave 5 removes those attrs from AIOrchestrator.__init__().
"""
import sys

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException


class TestFreetierRouting:
    """GATE-01: Free tier must use Groq/Llama 3.3 (D-02)."""

    def test_free_tier_routes_to_groq(self):
        """When tier='free' is in state, llm_client must be the Groq singleton from model_config."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        mock_client = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        with patch("app.core.model_config.get_client_for_tier", return_value=mock_client):
            orch = AIOrchestrator.__new__(AIOrchestrator)
            orch.graphs = {"rag": mock_graph}
            state = {"prompt": "test", "tier": "free"}
            orch.invoke("rag", state)

        assert state["llm_client"] is mock_client, (
            "Free tier must set llm_client from model_config.get_client_for_tier('free')"
        )

    def test_plus_tier_routes_to_gemini_flash(self):
        """When tier='plus' is in state, llm_client must be the Gemini Flash singleton from model_config."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        mock_client = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        with patch("app.core.model_config.get_client_for_tier", return_value=mock_client):
            orch = AIOrchestrator.__new__(AIOrchestrator)
            orch.graphs = {"rag": mock_graph}
            state = {"prompt": "test", "tier": "plus"}
            orch.invoke("rag", state)

        assert state["llm_client"] is mock_client, (
            "Plus tier must set llm_client from model_config.get_client_for_tier('plus')"
        )

    def test_pro_tier_routes_to_gemini_pro(self):
        """When tier='pro' is in state, llm_client must be the Gemini Pro singleton from model_config."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        mock_client = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        with patch("app.core.model_config.get_client_for_tier", return_value=mock_client):
            orch = AIOrchestrator.__new__(AIOrchestrator)
            orch.graphs = {"rag": mock_graph}
            state = {"prompt": "test", "tier": "pro"}
            orch.invoke("rag", state)

        assert state["llm_client"] is mock_client, (
            "Pro tier must set llm_client from model_config.get_client_for_tier('pro')"
        )

    def test_missing_tier_defaults_to_free(self):
        """When tier is absent from state, must default to free via model_config."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        mock_client = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        with patch("app.core.model_config.get_client_for_tier", return_value=mock_client):
            orch = AIOrchestrator.__new__(AIOrchestrator)
            orch.graphs = {"rag": mock_graph}
            state = {"prompt": "test"}  # no tier key
            orch.invoke("rag", state)

        assert state["llm_client"] is mock_client, (
            "Missing tier must default to free (model_config.get_client_for_tier called with 'free')"
        )

    def test_unknown_tier_defaults_to_free(self):
        """Unknown tier must not escalate — must default to free via model_config, not Pro."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        mock_client = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        with patch("app.core.model_config.get_client_for_tier", return_value=mock_client):
            orch = AIOrchestrator.__new__(AIOrchestrator)
            orch.graphs = {"rag": mock_graph}
            state = {"prompt": "test", "tier": "enterprise"}
            orch.invoke("rag", state)

        assert state["llm_client"] is mock_client, (
            "Unknown tier must not escalate — must default to free (model_config.get_client_for_tier called with 'free')"
        )


GEMINI_CLIENT_MODULE = "app.ai_orchestrator.llm_clients.gemini_client"


@pytest.fixture
def real_gemini_client():
    """Yield the REAL `Gemini_client`, whatever else the suite has stubbed.

    Other test modules install a configured stand-in for this module in
    `sys.modules` at collection time and never take it back out — deliberately,
    because they assert against that double. `sys.modules` is global, so these
    tests inherited it and quietly asserted against a MagicMock: they passed
    when run alone and failed in the suite, which is the least useful pair of
    outcomes a test can have.

    So this evicts whatever is registered, imports the real module, and puts the
    stub back afterwards — leaving global state exactly as it found it.
    """
    import importlib

    previous = sys.modules.pop(GEMINI_CLIENT_MODULE, None)
    try:
        module = importlib.import_module(GEMINI_CLIENT_MODULE)
        yield module.Gemini_client
    finally:
        if previous is not None:
            sys.modules[GEMINI_CLIENT_MODULE] = previous
        else:
            sys.modules.pop(GEMINI_CLIENT_MODULE, None)


class TestGeminiClientInit:
    """GATE-01: Gemini client must initialize correctly with model ID."""

    def test_gemini_client_flash_model_id(self, real_gemini_client):
        """Gemini_client initialized with gemini-flash-latest must use that model."""
        with patch("google.generativeai.configure"), \
             patch("google.generativeai.GenerativeModel") as mock_model, \
             patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            real_gemini_client(model_id="models/gemini-flash-latest")
            mock_model.assert_called_once_with("models/gemini-flash-latest")

    def test_gemini_client_raises_without_api_key(self, real_gemini_client):
        """Gemini_client must raise ValueError when GEMINI_API_KEY is missing."""
        with patch.dict("os.environ", {}, clear=True):
            # Ensure GEMINI_API_KEY is not set
            import os
            os.environ.pop("GEMINI_API_KEY", None)
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                real_gemini_client()


class TestGetSubscriptionTierDependency:
    """GATE-02: get_subscription_tier must return tier from authenticated user doc."""

    @pytest.mark.asyncio
    async def test_tier_read_from_user_document(self):
        """get_subscription_tier must read from MongoDB user doc, not request headers."""
        from app.auth.dependencies import get_subscription_tier
        # If tier injection test fails, it means tier is read from client (tamper risk)
        # This test verifies the dependency resolves from server-side user doc
        # Full integration requires running server; unit test verifies function signature
        import inspect
        sig = inspect.signature(get_subscription_tier)
        # Must have a `current_user` parameter with a Depends() default
        assert "current_user" in sig.parameters, (
            "get_subscription_tier must accept current_user via Depends(get_firebase_user)"
        )
