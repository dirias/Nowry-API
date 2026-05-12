from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.dependencies import track_ai_usage, get_subscription_tier
from app.utils.logger import get_logger

router = APIRouter(prefix="/visualizer", tags=["visualizer"])

logger = get_logger(__name__)


class VisualRequest(BaseModel):
    text: str
    viz_type: str = "mindmap"


@router.post("/generate")
async def generate_visual(
    request: VisualRequest,
    current_user: dict = Depends(track_ai_usage),
    tier: str = Depends(get_subscription_tier),
) -> dict:
    logger.info(f"[visualizer] tier={tier}")
    try:
        inputs = {"text": request.text, "viz_type": request.viz_type, "tier": tier}
        # Invoke via orchestrator
        result = orchestrator.invoke("visualizer", inputs)

        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])

        return {
            "mermaid_code": result.get("mermaid_code"),
            "explanation": result.get("explanation"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
