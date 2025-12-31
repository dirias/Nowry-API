from fastapi import APIRouter, HTTPException, Depends
from app.models.QuizGenerationRequest import QuizGenerationRequest
from app.ai_orchestrator.orchestrator import orchestrator
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/quiz",
    tags=["quiz"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


from app.auth.auth import get_current_user_authorization
from app.config.database import users_collection
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier
from bson import ObjectId


@router.post("/generate", summary="Generate a quiz from text")
async def generate_quiz(
    payload: QuizGenerationRequest,
    current_user: dict = Depends(get_current_user_authorization),
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
            detail=f"AI Quiz Generation is not available on the {plan['name']} plan. Upgrade to unlock.",
        )
    # --------------------------
    try:
        logger.info(
            f"Received quiz generation request with {payload.numQuestions} questions."
        )

        # Invoke the 'quiz' graph
        result = orchestrator.invoke(
            "quiz",
            {
                "sampleText": payload.sampleText,
                "numQuestions": payload.numQuestions,
                "difficulty": payload.difficulty,
                "prompt": payload.prompt,
            },
        )

        quiz_data = result.get("generated_quiz", [])
        logger.info(f"Quiz generation completed. Items: {len(quiz_data)}")
        return quiz_data

    except HTTPException as http_err:
        raise http_err
    except Exception as ex:
        logger.exception(f"Unexpected error during quiz generation: {ex}")
        raise HTTPException(status_code=500, detail=str(ex))
