from fastapi import APIRouter, HTTPException, Depends, Request
from app.core.limiter import limiter
from pydantic import BaseModel
from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.dependencies import track_ai_usage
from app.auth.firebase_auth import get_firebase_user
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/visualizer",
    tags=["visualizer"],
    dependencies=[Depends(get_firebase_user)],
)

logger = get_logger(__name__)


class VisualRequest(BaseModel):
    text: str
    viz_type: str = "mindmap"


@router.post("/generate")
@limiter.limit("10/minute")
async def generate_visual(
    request: Request,
    body: VisualRequest,
    current_user: dict = Depends(track_ai_usage),
) -> dict:
    # The monthly ceiling is enforced by track_ai_usage (GTM-003); `request` is
    # the HTTP request the rate limiter reads, `body` the visual to make.
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    logger.info(f"[visualizer] tier={tier}")
    try:
        inputs = {"text": body.text, "viz_type": body.viz_type, "tier": tier}
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
