# app/routers/card.py

from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from app.models.StudyCard import StudyCard
from app.models.CardGenerationRequest import CardGenerationRequest
from app.config.database import cards_collection
from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import track_ai_usage
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/card",
    tags=["cards"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_cards_collection() -> Collection:
    return cards_collection


@router.post("/generate", summary="Generate a new card using AI")
async def generate_card(
    payload: CardGenerationRequest,
    current_user: dict = Depends(track_ai_usage),
) -> dict:
    # TODO: AI usage limit enforcement is pending (Phase 4 deferred — WR-01)
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    logger.info(f"[cards] tier={tier}")
    try:
        logger.info(f"Received generation request: {payload}")
        result = orchestrator.invoke(
            "rag",
            {
                "prompt": payload.prompt,
                "sampleText": payload.sampleText,
                "sampleNumber": payload.sampleNumber,
                "tier": tier,
            },
        )
        logger.info("Card generation completed successfully.")
        return result["generated_cards"]
    except HTTPException as http_err:
        logger.error(f"Generation failed with HTTP error: {http_err.detail}")
        raise http_err
    except Exception as ex:
        logger.exception(f"Unexpected error during card generation: {ex}")
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/create", summary="Create a new card", response_model=StudyCard)
async def create_card(
    card: StudyCard,
    cards_collection: Collection = Depends(get_cards_collection),
    current_user: dict = Depends(get_firebase_user)
):
    logger.info(f"Creating card: {card.title}")
    
    card_dict = card.model_dump()
    # Security: Force user_id to be the authenticated user to prevent IDOR
    card_dict["user_id"] = current_user.get("user_id")
    
    result = await cards_collection.insert_one(card_dict)
    logger.info(f"Card created with ID: {result.inserted_id}")
    return {**card_dict, "id": str(result.inserted_id)}
