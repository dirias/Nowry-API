from fastapi import HTTPException
from typing import Dict, Any
from app.utils.logger import get_logger
from app.ai_orchestrator.rag.rag_graph import rag_app
from app.ai_orchestrator.quiz.quiz_graph import quiz_app
from app.ai_orchestrator.visualizer.visualizer_graph import visualizer_app
from app.ai_orchestrator.llm_clients.groq_client import Groq_client
from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client

logger = get_logger(__name__)


class AIOrchestrator:
    """Central controller for LangGraph pipelines."""

    def __init__(self):
        self.graphs = {
            "rag": rag_app,
            "quiz": quiz_app,
            "visualizer": visualizer_app,
        }
        # Initialize LLM clients once at startup; reused per request (D-02)
        self.groq_client = Groq_client()                                         # Free tier — Llama 3.3 70B
        self.gemini_flash_client = Gemini_client("models/gemini-flash-latest")   # Plus tier — Gemini Flash
        self.gemini_pro_client = Gemini_client("models/gemini-pro-latest")        # Pro tier — Gemini Pro

    def invoke(self, graph_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
        if graph_name not in self.graphs:
            logger.error(f"Graph '{graph_name}' not found.")
            raise HTTPException(status_code=404, detail=f"Unknown graph '{graph_name}'")

        graph = self.graphs[graph_name]

        # Model routing per tier (D-02) — tier must be set by route handler before invoking
        tier: str = state.get("tier", "free")
        if tier == "free":
            state["llm_client"] = self.groq_client          # Groq Llama 3.3 70B
        elif tier == "plus":
            state["llm_client"] = self.gemini_flash_client   # Gemini Flash
        else:  # pro — and any unknown tier defaults to Pro quality
            state["llm_client"] = self.gemini_pro_client     # Gemini Pro
        logger.info(f"[{graph_name}] tier={tier} — llm_client assigned")

        try:
            logger.info(f"[{graph_name}] Invoking pipeline with state: {state}")
            result = graph.invoke(state)
            logger.info(f"[{graph_name}] Completed successfully.")
            return result
        except Exception as e:
            logger.exception(f"[{graph_name}] Pipeline failed: {e}")
            raise HTTPException(
                status_code=500, detail=f"Pipeline '{graph_name}' failed due to: {e}"
            )


orchestrator = AIOrchestrator()
