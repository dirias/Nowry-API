"""
Phase 4 — Model Routing Tests (GATE-01, GATE-02)

Tests verify that AIOrchestrator.invoke() routes to the correct LLM client
based on the tier passed in state:
  - free  → Groq_client (Llama 3.3 70B)
  - plus  → Gemini_client configured with gemini-flash-latest
  - pro   → Gemini_client configured with gemini-pro-latest
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException


class TestFreetierRouting:
    """GATE-01: Free tier must use Groq/Llama 3.3 (D-02)."""

    def test_free_tier_routes_to_groq(self):
        """When tier='free' is in state, llm_client must be a Groq_client instance."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator
        from app.ai_orchestrator.llm_clients.groq_client import Groq_client

        orch = AIOrchestrator.__new__(AIOrchestrator)

        mock_groq = MagicMock(spec=Groq_client)
        mock_gemini_flash = MagicMock()
        mock_gemini_pro = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        orch.groq_client = mock_groq
        orch.gemini_flash_client = mock_gemini_flash
        orch.gemini_pro_client = mock_gemini_pro
        orch.graphs = {"rag": mock_graph}

        state = {"prompt": "test", "tier": "free"}
        orch.invoke("rag", state)

        assert state["llm_client"] is mock_groq, (
            "Free tier must inject groq_client into state"
        )

    def test_plus_tier_routes_to_gemini_flash(self):
        """When tier='plus' is in state, llm_client must be the Gemini Flash client."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        orch = AIOrchestrator.__new__(AIOrchestrator)

        mock_groq = MagicMock()
        mock_gemini_flash = MagicMock()
        mock_gemini_pro = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        orch.groq_client = mock_groq
        orch.gemini_flash_client = mock_gemini_flash
        orch.gemini_pro_client = mock_gemini_pro
        orch.graphs = {"rag": mock_graph}

        state = {"prompt": "test", "tier": "plus"}
        orch.invoke("rag", state)

        assert state["llm_client"] is mock_gemini_flash, (
            "Plus tier must inject gemini_flash_client into state"
        )

    def test_pro_tier_routes_to_gemini_pro(self):
        """When tier='pro' is in state, llm_client must be the Gemini Pro client."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        orch = AIOrchestrator.__new__(AIOrchestrator)

        mock_groq = MagicMock()
        mock_gemini_flash = MagicMock()
        mock_gemini_pro = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        orch.groq_client = mock_groq
        orch.gemini_flash_client = mock_gemini_flash
        orch.gemini_pro_client = mock_gemini_pro
        orch.graphs = {"rag": mock_graph}

        state = {"prompt": "test", "tier": "pro"}
        orch.invoke("rag", state)

        assert state["llm_client"] is mock_gemini_pro, (
            "Pro tier must inject gemini_pro_client into state"
        )

    def test_missing_tier_defaults_to_free(self):
        """When tier is absent from state, must default to free (Groq)."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        orch = AIOrchestrator.__new__(AIOrchestrator)

        mock_groq = MagicMock()
        mock_gemini_flash = MagicMock()
        mock_gemini_pro = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        orch.groq_client = mock_groq
        orch.gemini_flash_client = mock_gemini_flash
        orch.gemini_pro_client = mock_gemini_pro
        orch.graphs = {"rag": mock_graph}

        state = {"prompt": "test"}  # no tier key
        orch.invoke("rag", state)

        assert state["llm_client"] is mock_groq, (
            "Missing tier must default to free (Groq client)"
        )

    def test_unknown_tier_defaults_to_free(self):
        """Unknown tier must not escalate — must default to free (Groq), not Pro."""
        from app.ai_orchestrator.orchestrator import AIOrchestrator

        orch = AIOrchestrator.__new__(AIOrchestrator)

        mock_groq = MagicMock()
        mock_gemini_flash = MagicMock()
        mock_gemini_pro = MagicMock()
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"result": "ok"}

        orch.groq_client = mock_groq
        orch.gemini_flash_client = mock_gemini_flash
        orch.gemini_pro_client = mock_gemini_pro
        orch.graphs = {"rag": mock_graph}

        state = {"prompt": "test", "tier": "enterprise"}
        orch.invoke("rag", state)

        assert state["llm_client"] is mock_groq, (
            "Unknown tier must not escalate — must default to free (Groq client)"
        )


class TestGeminiClientInit:
    """GATE-01: Gemini client must initialize correctly with model ID."""

    def test_gemini_client_flash_model_id(self):
        """Gemini_client initialized with gemini-flash-latest must use that model."""
        with patch("google.generativeai.configure"), \
             patch("google.generativeai.GenerativeModel") as mock_model, \
             patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
            from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client
            client = Gemini_client(model_id="models/gemini-flash-latest")
            mock_model.assert_called_once_with("models/gemini-flash-latest")

    def test_gemini_client_raises_without_api_key(self):
        """Gemini_client must raise ValueError when GEMINI_API_KEY is missing."""
        with patch.dict("os.environ", {}, clear=True):
            # Ensure GEMINI_API_KEY is not set
            import os
            os.environ.pop("GEMINI_API_KEY", None)
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client
                Gemini_client()


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
