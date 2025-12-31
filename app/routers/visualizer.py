from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.auth import get_current_user_authorization
from app.config.database import users_collection
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier
from bson import ObjectId

router = APIRouter(prefix="/visualizer", tags=["visualizer"])


class VisualRequest(BaseModel):
    text: str
    viz_type: str = "mindmap"


@router.post("/generate")
async def generate_visual(
    request: VisualRequest, current_user: dict = Depends(get_current_user_authorization)
):
    # --- Subscription Check ---
    user_id = current_user.get("user_id")
    user = await users_collection.find_one({"_id": ObjectId(user_id)})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    subscription_data = user.get("subscription", {"tier": "free"})
    tier_key = subscription_data.get("tier", "free")
    plan = SUBSCRIPTION_PLANS.get(tier_key, SUBSCRIPTION_PLANS[SubscriptionTier.FREE])

    if not plan["features"]["ai_content_generation"]:
        raise HTTPException(
            status_code=403,
            detail=f"AI Visualization is not available on the {plan['name']} plan. Upgrade to unlock.",
        )
    # --------------------------
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
