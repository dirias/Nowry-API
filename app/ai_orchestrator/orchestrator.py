from fastapi import HTTPException
from typing import Dict, Any
from app.utils.logger import get_logger
from app.ai_orchestrator.rag.rag_graph import rag_app

logger = get_logger(__name__)


class AIOrchestrator:
    """Central controller for LangGraph pipelines."""

    def __init__(self):
        self.graphs = {
            "rag": rag_app,
        }

    def invoke(self, graph_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
        if graph_name not in self.graphs:
            logger.error(f"Graph '{graph_name}' not found.")
            raise HTTPException(status_code=404, detail=f"Unknown graph '{graph_name}'")

        graph = self.graphs[graph_name]

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
