# app/routers/card.py

from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from app.models.StudyCard import StudyCard
from app.models.CardGenerationRequest import CardGenerationRequest
from app.config.database import cards_collection
from app.ai_orchestrator.orchestrator import orchestrator
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/card",
    tags=["cards"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


def get_cards_collection() -> Collection:
    return cards_collection


@router.post("/generate", summary="Generate a new card using AI")
async def generate_card(payload: CardGenerationRequest):
    try:
        logger.info(f"Received generation request: {payload}")
        result = orchestrator.invoke(
            "rag",
            {
                "prompt": payload.prompt,
                "sampleText": payload.sampleText,
                "sampleNumber": payload.sampleNumber,
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
    card: StudyCard, cards_collection: Collection = Depends(get_cards_collection)
):
    logger.info(f"Creating card: {card.title}")
    result = await cards_collection.insert_one(card.dict())
    logger.info(f"Card created with ID: {result.inserted_id}")
    return {**card.dict(), "id": str(result.inserted_id)}
