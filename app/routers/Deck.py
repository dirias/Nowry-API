from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.collection import Collection
from uuid import UUID
from app.models.Deck import Deck
from app.config.database import decks_collection
from app.utils.logger import get_logger

router = APIRouter(prefix="/users", tags=["decks"])
logger = get_logger(__name__)

def get_decks_collection() -> Collection:
    return decks_collection

@router.get(
    "/{user_id}/decks",
    summary="Retrieve all decks belonging to a user (not soft-deleted)",
    response_model=list[Deck],
    status_code=status.HTTP_200_OK,
)
async def get_user_decks(user_id: str, collection: Collection = Depends(get_decks_collection)):
    # 1️⃣ Validar que sea un UUID
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    # 2️⃣ Consultar decks activos
    query = {"user_id": str(user_uuid), "deleted_at": None}
    logger.info(f"Fetching decks for user {user_id} with query: {query}")

    decks = await collection.find(query).to_list(length=None)

    # 3️⃣ Devolver respuesta
    logger.info(f"Found {len(decks)} decks for user {user_id}")
    return decks
