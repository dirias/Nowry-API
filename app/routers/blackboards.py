from fastapi import APIRouter, Depends
from app.auth.firebase_auth import get_firebase_user
from app.config.database import db
from app.models.Blackboard import BlackboardUpdate
from app.models.common import BlackboardResponse, OkResponse
from bson import ObjectId
from datetime import datetime, timezone

router = APIRouter(prefix="/blackboards", tags=["blackboards"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("/{board_id}", response_model=BlackboardResponse)
async def get_blackboard(board_id: str, current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")

    # Try by Mongo _id first, then by board_id string
    doc = None
    try:
        doc = await db.blackboards.find_one({"_id": ObjectId(board_id), "user_id": user_id})
    except Exception:
        pass

    if not doc:
        doc = await db.blackboards.find_one({"board_id": board_id, "user_id": user_id})

    if not doc:
        # Auto-create on first access
        now = datetime.now(timezone.utc)
        new_board = {
            "board_id": board_id,
            "user_id": user_id,
            "name": "My Blackboard",
            "nodes": [],
            "edges": [],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "created_at": now,
            "updated_at": now,
        }
        result = await db.blackboards.insert_one(new_board)
        new_board["_id"] = result.inserted_id
        doc = new_board

    return _serialize(doc)


@router.put("/{board_id}", response_model=BlackboardResponse)
async def save_blackboard(
    board_id: str,
    update: BlackboardUpdate,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)

    update_fields = {"updated_at": now}
    if update.name is not None:
        update_fields["name"] = update.name
    if update.nodes is not None:
        update_fields["nodes"] = update.nodes
    if update.edges is not None:
        update_fields["edges"] = update.edges
    if update.viewport is not None:
        update_fields["viewport"] = update.viewport

    result = await db.blackboards.find_one_and_update(
        {"board_id": board_id, "user_id": user_id},
        {"$set": update_fields},
        return_document=True,
        upsert=True,
    )
    return _serialize(result)


@router.delete("/{board_id}", response_model=OkResponse)
async def clear_blackboard(board_id: str, current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)

    await db.blackboards.update_one(
        {"board_id": board_id, "user_id": user_id},
        {"$set": {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}, "updated_at": now}},
    )
    return {"ok": True}
