from fastapi import HTTPException
from typing import Dict, Any
from app.utils.logger import get_logger
from app.ai_orchestrator.rag.rag_graph import rag_app
from app.ai_orchestrator.quiz.quiz_graph import quiz_app
from app.ai_orchestrator.visualizer.visualizer_graph import visualizer_app
from app.core import model_config

logger = get_logger(__name__)


class AIOrchestrator:
    """Central controller for LangGraph pipelines."""

    def __init__(self):
        self.graphs = {
            "rag": rag_app,
            "quiz": quiz_app,
            "visualizer": visualizer_app,
        }
        # LLM clients moved to module-level singletons in app.core.model_config (D-13)

    def invoke(self, graph_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
        if graph_name not in self.graphs:
            logger.error(f"Graph '{graph_name}' not found.")
            raise HTTPException(status_code=404, detail=f"Unknown graph '{graph_name}'")

        graph = self.graphs[graph_name]

        # Model routing per tier (D-13) — delegates to centralized model_config singleton
        tier: str = state.get("tier", "free")
        state["llm_client"] = model_config.get_client_for_tier(tier)
        logger.info(f"[{graph_name}] tier={tier} — llm_client assigned")

        try:
            logger.info(f"[{graph_name}] Invoking pipeline with state: {state}")
            result = graph.invoke(state)
            logger.info(f"[{graph_name}] Completed successfully.")
            return result
        except Exception as e:
            logger.exception(f"[{graph_name}] Pipeline failed: {e}")
            raise HTTPException(
                status_code=500, detail="AI pipeline failed. Please try again."
            )


orchestrator = AIOrchestrator()
