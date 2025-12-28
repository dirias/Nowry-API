from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.ai_orchestrator.orchestrator import orchestrator

router = APIRouter(prefix="/visualizer", tags=["visualizer"])


class VisualRequest(BaseModel):
    text: str
    viz_type: str = "mindmap"


@router.post("/generate")
async def generate_visual(request: VisualRequest):
    try:
        inputs = {"text": request.text, "viz_type": request.viz_type}
        # Invoke via orchestrator
        result = orchestrator.invoke("visualizer", inputs)

        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])

        return {
            "mermaid_code": result.get("mermaid_code"),
            "explanation": result.get("explanation"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
