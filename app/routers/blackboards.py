import asyncio
import html
import re
from fastapi import APIRouter, Depends, HTTPException
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import get_subscription_tier
from app.config.database import db
from app.config.subscription_plans import SubscriptionTier
from app.models.Blackboard import (
    BlackboardUpdate,
    CreateBoardRequest,
    InviteCollaboratorRequest,
    BoardToCardRequest,
    BoardToCardResponse,
)
from app.models.common import BlackboardResponse, OkResponse
from app.ai_orchestrator.orchestrator import orchestrator
from bson import ObjectId
from datetime import datetime, timezone

router = APIRouter(prefix="/blackboards", tags=["blackboards"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


async def _resolve_owner_names(boards) -> dict:
    """Map owner_user_id -> display name for a batch of board documents.

    One bounded `$in` query instead of an N+1 per-board lookup. Ids that are not
    valid ObjectIds (or that no longer resolve to a user) are simply absent from
    the returned map; callers substitute "".
    """
    owner_ids = {
        board.get("owner_user_id")
        for board in boards
        if board.get("owner_user_id")
    }
    if not owner_ids:
        return {}

    object_ids = []
    for owner_id in owner_ids:
        try:
            object_ids.append(ObjectId(owner_id))
        except Exception:
            continue
    if not object_ids:
        return {}

    users = await db.users.find({"_id": {"$in": object_ids}}).to_list(length=100)
    return {
        str(user["_id"]): (user.get("username") or user.get("email") or "")
        for user in users
    }


def _clean_node_text(raw: str, max_chars: int = 500) -> str:
    """Strip HTML tags and entities from node text, then cap at max_chars."""
    unescaped = html.unescape(raw)
    no_tags = re.sub(r'<[^>]+>', ' ', unescaped)
    collapsed = ' '.join(no_tags.split())
    return collapsed[:max_chars]


# ── Phase 7: Board limits per tier ──────────────────────────────────────────
_BOARD_LIMITS = {
    SubscriptionTier.FREE: 1,
    SubscriptionTier.PLUS: 1,
    SubscriptionTier.PRO: -1,   # -1 means unlimited
}


# ── Phase 7: POST /blackboards — Create a named board ──────────────────────
@router.post("", response_model=BlackboardResponse)
async def create_board(
    body: CreateBoardRequest,
    current_user: dict = Depends(get_firebase_user),
    tier: str = Depends(get_subscription_tier),
):
    user_id = current_user.get("user_id")

    # Resolve tier enum for limit lookup (tier is a plain string from dependency)
    try:
        tier_enum = SubscriptionTier(tier)
    except ValueError:
        tier_enum = SubscriptionTier.FREE

    max_boards = _BOARD_LIMITS.get(tier_enum, 1)

    if max_boards != -1:
        count = await db.blackboards.count_documents({"owner_user_id": user_id})
        if count >= max_boards:
            raise HTTPException(status_code=402, detail="board_limit_reached")

    now = datetime.now(timezone.utc)
    new_board = {
        "owner_user_id": user_id,
        "user_id": user_id,         # backward compat field
        "collaborators": [],
        "name": body.name,
        "nodes": [],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "created_at": now,
        "updated_at": now,
    }
    result = await db.blackboards.insert_one(new_board)
    new_board["_id"] = result.inserted_id
    return _serialize(new_board)


# ── Phase 7: GET /blackboards — List owned + shared boards ─────────────────
@router.get("", response_model=list[BlackboardResponse])
async def list_boards(current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")

    # "deleted_at": None also matches documents where the field is absent, so
    # boards predating the soft-delete field still list normally. Boards whose
    # owner deleted their account are soft-deleted by the users.py cascade and
    # therefore drop out of every collaborator's list too (no orphaned boards).
    owned = await db.blackboards.find(
        {"owner_user_id": user_id, "deleted_at": None}
    ).to_list(length=100)
    shared = await db.blackboards.find(
        {"collaborators": user_id, "deleted_at": None}
    ).to_list(length=100)

    # Merge, deduplicating by _id (owned takes precedence)
    all_boards: dict = {doc["_id"]: doc for doc in owned}
    for doc in shared:
        all_boards.setdefault(doc["_id"], doc)

    owner_names = await _resolve_owner_names(all_boards.values())

    result = []
    for doc in all_boards.values():
        doc["is_owner"] = (doc.get("owner_user_id") == user_id) or (
            doc.get("user_id") == user_id and doc.get("owner_user_id") is None
        )
        doc["owner_name"] = owner_names.get(doc.get("owner_user_id"), "")
        result.append(_serialize(doc))
    return result


# ── Existing: GET /{board_id} — Retrieve a single board ────────────────────
@router.get("/{board_id}", response_model=BlackboardResponse)
async def get_blackboard(board_id: str, current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")

    # Two-step lookup: ObjectId first, then legacy board_id string (same as PUT).
    # The caller's user_id is deliberately NOT part of either Mongo filter — a
    # collaborator is not the owner, so filtering on user_id here would 404 every
    # shared board. Access is enforced by the guard below instead.
    # "deleted_at": None keeps soft-deleted (orphaned) boards out of both steps.
    doc = None
    try:
        doc = await db.blackboards.find_one(
            {"_id": ObjectId(board_id), "deleted_at": None}
        )
    except Exception:
        pass

    if not doc:
        doc = await db.blackboards.find_one({"board_id": board_id, "deleted_at": None})

    if not doc:
        raise HTTPException(status_code=404, detail="board_not_found")

    if doc.get("owner_user_id") is not None:
        # Phase 7 board: enforce owner/collaborator guard
        owner = doc.get("owner_user_id")
        collaborators = doc.get("collaborators", [])
        if owner != user_id and user_id not in collaborators:
            raise HTTPException(status_code=403, detail="board_access_denied")

    # Legacy boards (no owner_user_id): allow read for backward compat

    return _serialize(doc)


# ── Phase 7: PUT /{board_id} — Save with owner/collaborator guard ──────────
@router.put("/{board_id}", response_model=BlackboardResponse)
async def save_blackboard(
    board_id: str,
    update: BlackboardUpdate,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)

    # Fetch existing board to check ownership (Phase 7 guard)
    existing = None
    try:
        existing = await db.blackboards.find_one({"_id": ObjectId(board_id)})
    except Exception:
        pass

    if not existing:
        # Fall back to legacy board_id string field
        existing = await db.blackboards.find_one({"board_id": board_id})

    if not existing:
        raise HTTPException(status_code=404, detail="board_not_found")

    if existing.get("owner_user_id") is not None:
        # Phase 7 board: enforce owner/collaborator guard
        owner = existing.get("owner_user_id")
        collaborators = existing.get("collaborators", [])
        if owner != user_id and user_id not in collaborators:
            raise HTTPException(status_code=403, detail="board_access_denied")

    # Legacy boards (no owner_user_id): allow save for backward compat

    update_fields: dict = {"updated_at": now}
    if update.name is not None:
        update_fields["name"] = update.name
    if update.nodes is not None:
        update_fields["nodes"] = update.nodes
    if update.edges is not None:
        update_fields["edges"] = update.edges
    if update.viewport is not None:
        update_fields["viewport"] = update.viewport

    result = await db.blackboards.find_one_and_update(
        {"_id": existing["_id"]},
        {"$set": update_fields},
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="board_not_found")
    return _serialize(result)


# ── Phase 7: PUT /{board_id}/invite — Add a collaborator ───────────────────
@router.put("/{board_id}/invite", response_model=OkResponse)
async def invite_collaborator(
    board_id: str,
    body: InviteCollaboratorRequest,
    current_user: dict = Depends(get_firebase_user),
):
    user_id = current_user.get("user_id")

    # Fetch the board
    board = None
    try:
        board = await db.blackboards.find_one({"_id": ObjectId(board_id)})
    except Exception:
        pass

    if not board:
        raise HTTPException(status_code=404, detail="board_not_found")

    # Only the board owner may invite collaborators
    if board.get("owner_user_id") != user_id:
        raise HTTPException(status_code=403, detail="only_owner_can_invite")

    # Look up the invitee by email in the users collection (T-07-02-05 mitigation)
    invitee = await db.users.find_one({"email": body.invitee_email})
    if not invitee:
        raise HTTPException(status_code=404, detail="user_not_found")

    invitee_id = str(invitee["_id"])
    await db.blackboards.update_one(
        {"_id": ObjectId(board_id)},
        {"$addToSet": {"collaborators": invitee_id}},
    )
    return {"ok": True}


# ── Phase 7: POST /{board_id}/generate-cards — Board-to-card generation ────
@router.post("/{board_id}/generate-cards", response_model=BoardToCardResponse)
async def generate_cards_from_board(
    board_id: str,
    body: BoardToCardRequest,
    current_user: dict = Depends(get_firebase_user),
    tier: str = Depends(get_subscription_tier),
):
    user_id = current_user.get("user_id")

    # Access guard: owner or collaborator (T-07-02-02 mitigation)
    # Two-step lookup: ObjectId first, then legacy board_id string (same as GET/PUT)
    board = None
    try:
        board = await db.blackboards.find_one({"_id": ObjectId(board_id)})
    except Exception:
        pass
    if not board:
        board = await db.blackboards.find_one({"board_id": board_id, "user_id": user_id})

    if not board:
        raise HTTPException(status_code=404, detail="board_not_found")

    if board.get("owner_user_id") is not None:
        owner = board.get("owner_user_id")
        collaborators = board.get("collaborators", [])
        if owner != user_id and user_id not in collaborators:
            raise HTTPException(status_code=403, detail="board_access_denied")

    # Cap at 20 nodes (T-07-02-03 mitigation)
    nodes_truncated = len(body.node_texts) > 20
    selected_texts = body.node_texts[:20]

    # Strip HTML and filter empty nodes (_clean_node_text handles T-07-02-03)
    cleaned = [_clean_node_text(t) for t in selected_texts if t.strip()]
    if not cleaned:
        raise HTTPException(status_code=422, detail="no_extractable_text")

    # Assemble prompt text — cap total at 8000 chars
    node_text = "\n\n".join(f"Node {i + 1}: {t}" for i, t in enumerate(cleaned))
    if len(node_text) > 8000:
        node_text = node_text[:8000]
        nodes_truncated = True

    # Invoke orchestrator via the "rag" graph (same pipeline as POST /cards/generate)
    # T-07-02-04: asyncio.wait_for with 30s timeout (LLM latency can be 10-20s)
    tier_str = tier if isinstance(tier, str) else tier.value
    state = {
        "prompt": "Generate flashcards from the following board notes. Each card should have a clear question (front) and a concise answer (back).",
        "sampleText": node_text,
        "sampleNumber": min(len(cleaned), 10),
        "tier": tier_str,
    }
    cards = []
    for attempt in range(3):
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(orchestrator.invoke, "rag", state),
                timeout=30.0,
            )
            cards = result.get("generated_cards") or []
            if cards:
                break
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="card_generation_timeout")
        except Exception:
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))

    if not cards:
        raise HTTPException(status_code=502, detail="card_generation_parse_failed")

    return BoardToCardResponse(
        cards=cards,
        node_count=len(body.node_ids),
        nodes_truncated=nodes_truncated,
    )


# ── Existing: DELETE /{board_id} — Clear a board ───────────────────────────
@router.delete("/{board_id}", response_model=OkResponse)
async def clear_blackboard(board_id: str, current_user: dict = Depends(get_firebase_user)):
    user_id = current_user.get("user_id")
    now = datetime.now(timezone.utc)

    # Two-step lookup: ObjectId first, then legacy board_id string (same as GET/PUT).
    # Every board created since Phase 7 has only an _id — the old
    # {"board_id": ..., "user_id": ...} filter matched zero documents, so the
    # clear silently no-op'd while still returning ok.
    board = None
    try:
        board = await db.blackboards.find_one({"_id": ObjectId(board_id)})
    except Exception:
        pass

    if not board:
        board = await db.blackboards.find_one({"board_id": board_id})

    if not board:
        raise HTTPException(status_code=404, detail="board_not_found")

    if board.get("owner_user_id") is not None:
        # Phase 7 board: enforce owner/collaborator guard. Required alongside the
        # lookup fix — without it, making the clear actually work would let any
        # authenticated caller wipe any board by id.
        owner = board.get("owner_user_id")
        collaborators = board.get("collaborators", [])
        if owner != user_id and user_id not in collaborators:
            raise HTTPException(status_code=403, detail="board_access_denied")

    # Legacy boards (no owner_user_id): allow clear for backward compat

    await db.blackboards.update_one(
        {"_id": board["_id"]},
        {"$set": {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}, "updated_at": now}},
    )
    return {"ok": True}
