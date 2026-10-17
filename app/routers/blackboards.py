"""Boards (ADR-047, docs/prd-boards.md FR-001).

A board is read whole (GET), saved by change (PATCH, merged by id with a
revision), listed with counts and a thumbnail outline, duplicated and
soft-deleted. A board stored before ADR-047 is migrated on its first GET; the
pure builders live in `app/services/boards.py`. The whole-document PUT answers
410.
"""
import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.dependencies import get_subscription_tier
from app.auth.firebase_auth import get_firebase_user
from app.config.database import db
from app.config.subscription_plans import SubscriptionTier
from app.models.Blackboard import (
    BoardListItem,
    BoardPatch,
    BoardPatchResponse,
    BoardResponse,
    BoardToCardRequest,
    BoardToCardResponse,
    CreateBoardRequest,
    InviteCollaboratorRequest,
)
from app.models.common import OkResponse
from app.services import boards as svc

router = APIRouter(prefix="/blackboards", tags=["blackboards"])

#: A PATCH re-reads and re-merges when another save landed between its read
#: and its write; after this many tries it gives up with 409.
SAVE_TRIES = 3
BOARDS_GONE = "Boards save by change (ADR-047); use PATCH"


# ── Reading and guarding ────────────────────────────────────────────────────


def _owner_of(doc: dict) -> Optional[str]:
    """`owner_user_id`, or the legacy single-board `user_id` before Phase 7."""
    return doc.get("owner_user_id") or doc.get("user_id")


def _can_edit(doc: dict, user_id: str) -> bool:
    return _owner_of(doc) == user_id or user_id in (doc.get("collaborators") or [])


async def _find_board(board_id: str, user_id: str) -> dict:
    """The live board by `_id`, else the caller's legacy board by `board_id`.

    The `_id` lookup deliberately does not filter on the caller: a collaborator
    is not the owner, and access is the guard's job. A legacy `board_id` (e.g.
    "main") is shared by every legacy user, so that lookup is the caller's own.
    """
    doc = None
    if ObjectId.is_valid(board_id):
        doc = await db.blackboards.find_one({"_id": ObjectId(board_id), "deleted_at": None})
    if not doc:
        doc = await db.blackboards.find_one({"board_id": board_id, "user_id": user_id, "deleted_at": None})
    if not doc:
        raise HTTPException(status_code=404, detail="board_not_found")
    return doc


async def _editable_board(board_id: str, user_id: str) -> dict:
    """The board, when the caller owns it or collaborates on it; 403 otherwise."""
    doc = await _find_board(board_id, user_id)
    if not _can_edit(doc, user_id):
        raise HTTPException(status_code=403, detail="board_access_denied")
    return doc


async def _priority_refs(docs: list[dict]) -> dict[str, Optional[dict]]:
    """{priority id: item ref or None} for every priority a v1 board points at.

    One bounded `$in` over both id forms (ObjectId and string); an id that
    resolves to nothing maps to None, and its entity becomes a note.
    """
    ids = svc.priority_ids(docs)
    if not ids:
        return {}
    keys: list = ids + [ObjectId(value) for value in ids if ObjectId.is_valid(value)]
    rows = await db.priorities.find({"_id": {"$in": keys}}).to_list(length=len(keys))
    refs: dict[str, Optional[dict]] = {value: None for value in ids}
    for row in rows:
        refs[str(row["_id"])] = svc.priority_ref(row)
    return refs


async def _content(doc: dict) -> tuple[list[dict], list[dict]]:
    """A board's v2 nodes and links, migrating a v1 board in memory."""
    refs = {} if svc.is_v2(doc) else await _priority_refs([doc])
    return svc.board_content(doc, refs)


def _board_response(doc: dict, names: Optional[dict[str, str]] = None) -> BoardResponse:
    """A v2 document as the wire shape; `names` from `_resolve_names`, none for
    a board just written by its owner (no collaborators yet)."""
    names = names or {}
    return BoardResponse(
        id=str(doc["_id"]),
        name=doc.get("name") or "",
        owner_user_id=_owner_of(doc),
        owner_name=names.get(doc.get("owner_user_id"), ""),
        collaborators=list(doc.get("collaborators") or []),
        collaborator_names=_collaborator_names(doc, names),
        rev=int(doc.get("rev") or 1),
        nodes=doc.get("nodes") or [],
        links=doc.get("links") or [],
        viewport=svc.viewport_of(doc),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


#: The most user ids one name lookup resolves: owners first, then collaborators.
NAME_LOOKUP_CAP = 500


def _people_of(boards: list[dict]) -> list[str]:
    """The owner and collaborator ids of a batch of boards, owners first,
    each once, in a stable order, capped at NAME_LOOKUP_CAP."""
    owners = {board["owner_user_id"] for board in boards if board.get("owner_user_id")}
    collaborators = {
        user_id for board in boards for user_id in board.get("collaborators") or [] if user_id
    }
    people = sorted(owners) + sorted(collaborators - owners)
    return people[:NAME_LOOKUP_CAP]


async def _resolve_names(boards: list[dict]) -> dict[str, str]:
    """{user id: display name} for every owner and collaborator of `boards`.

    One bounded `$in` query instead of an N+1 per-board lookup. Ids that are not
    valid ObjectIds, that no longer resolve to a user, or whose user has neither
    a username nor an email are absent from the map; callers substitute "" for
    an owner and leave a collaborator out.
    """
    object_ids = [ObjectId(user_id) for user_id in _people_of(boards) if ObjectId.is_valid(user_id)]
    if not object_ids:
        return {}
    users = await db.users.find({"_id": {"$in": object_ids}}).to_list(length=len(object_ids))
    names = {str(user["_id"]): user.get("username") or user.get("email") or "" for user in users}
    return {user_id: name for user_id, name in names.items() if name}


def _collaborator_names(doc: dict, names: dict[str, str]) -> dict[str, str]:
    """{collaborator id: name} for the board's collaborators that resolved."""
    return {user_id: names[user_id] for user_id in doc.get("collaborators") or [] if user_id in names}


def _clean_node_text(raw: str, max_chars: int = 500) -> str:
    """Strip HTML tags and entities from node text, then cap at max_chars."""
    unescaped = html.unescape(raw)
    no_tags = re.sub(r'<[^>]+>', ' ', unescaped)
    collapsed = ' '.join(no_tags.split())
    return collapsed[:max_chars]


# ── Plans: how many boards a caller may own ─────────────────────────────────
_BOARD_LIMITS = {
    SubscriptionTier.FREE: 1,
    SubscriptionTier.PLUS: 1,
    SubscriptionTier.PRO: -1,   # -1 means unlimited
}


async def _check_board_limit(user_id: str, tier: str) -> None:
    """402 when the caller already owns as many live boards as the plan allows."""
    try:
        tier_enum = SubscriptionTier(tier)
    except ValueError:
        tier_enum = SubscriptionTier.FREE
    max_boards = _BOARD_LIMITS.get(tier_enum, 1)
    if max_boards == -1:
        return
    count = await db.blackboards.count_documents({"owner_user_id": user_id, "deleted_at": None})
    if count >= max_boards:
        raise HTTPException(status_code=402, detail="board_limit_reached")


async def _insert_board(user_id: str, name: str, nodes: list, links: list, viewport: dict) -> dict:
    """Write a new v2 board owned by the caller and return the stored document."""
    now = datetime.now(timezone.utc)
    new_board = {
        "owner_user_id": user_id,
        "user_id": user_id,         # backward compat field
        "collaborators": [],
        "name": name,
        "schema": svc.SCHEMA,
        "rev": 1,
        "nodes": nodes,
        "links": links,
        "viewport": viewport,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "deleted_by": None,
    }
    result = await db.blackboards.insert_one(new_board)
    new_board["_id"] = result.inserted_id
    return new_board


# ── POST /blackboards — create a board ─────────────────────────────────────
@router.post("", response_model=BoardResponse)
async def create_board(
    body: CreateBoardRequest,
    current_user: dict = Depends(get_firebase_user),
    tier: str = Depends(get_subscription_tier),
) -> BoardResponse:
    user_id = current_user.get("user_id")
    await _check_board_limit(user_id, tier)
    doc = await _insert_board(user_id, body.name, [], [], dict(svc.DEFAULT_VIEWPORT))
    return _board_response(doc)


# ── GET /blackboards — owned and shared boards, summarised ─────────────────
@router.get("", response_model=list[BoardListItem])
async def list_boards(current_user: dict = Depends(get_firebase_user)) -> list[BoardListItem]:
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
    boards = list(all_boards.values())

    names = await _resolve_names(boards)
    # A v1 board is summarised through the migration builders and NOT written:
    # the write belongs to its first GET.
    refs = await _priority_refs(boards)
    return [_list_item(doc, user_id, names, refs) for doc in boards]


def _list_item(doc: dict, user_id: str, names: dict[str, str], refs: dict) -> BoardListItem:
    nodes, links = svc.board_content(doc, refs)
    return BoardListItem(
        id=str(doc["_id"]),
        name=doc.get("name") or "",
        owner_user_id=_owner_of(doc),
        owner_name=names.get(doc.get("owner_user_id"), ""),
        collaborators=list(doc.get("collaborators") or []),
        collaborator_names=_collaborator_names(doc, names),
        is_owner=_owner_of(doc) == user_id,
        updated_at=doc.get("updated_at"),
        counts=svc.counts(nodes, links),
        outline=svc.outline(nodes),
    )


# ── GET /{board_id} — one board, migrated to v2 on its first read ──────────
@router.get("/{board_id}", response_model=BoardResponse)
async def get_blackboard(
    board_id: str, current_user: dict = Depends(get_firebase_user)
) -> BoardResponse:
    doc = await _editable_board(board_id, current_user.get("user_id"))
    if not svc.is_v2(doc):
        doc = await _migrate_in_place(doc)
    return _board_response(doc, await _resolve_names([doc]))


async def _migrate_in_place(doc: dict) -> dict:
    """Store a v1 board as v2 (D8): schema 2, rev 1, nodes and links replaced,
    `edges` unset. The filter skips a board someone else migrated meanwhile,
    in which case that stored v2 board is what is returned."""
    nodes, links = svc.board_content(doc, await _priority_refs([doc]))
    fields = {
        "schema": svc.SCHEMA, "rev": 1, "nodes": nodes, "links": links,
        "viewport": svc.viewport_of(doc),
    }
    result = await db.blackboards.update_one(
        {"_id": doc["_id"], "schema": {"$ne": svc.SCHEMA}},
        {"$set": fields, "$unset": {"edges": ""}},
    )
    if not result.matched_count:
        fresh = await db.blackboards.find_one({"_id": doc["_id"], "deleted_at": None})
        if fresh and svc.is_v2(fresh):
            return fresh
    migrated = {key: value for key, value in doc.items() if key != "edges"}
    migrated.update(fields)
    return migrated


# ── PATCH /{board_id} — save by change (D5) ────────────────────────────────
@router.patch("/{board_id}", response_model=BoardPatchResponse)
async def patch_board(
    board_id: str,
    body: BoardPatch,
    current_user: dict = Depends(get_firebase_user),
) -> BoardPatchResponse:
    """Merge the changes by id into whatever is stored and bump `rev`. The
    write is conditional on the revision read; a save that lands in between
    makes this one re-read and re-merge, up to three tries, then 409."""
    user_id = current_user.get("user_id")
    svc.check_change_count(body)
    for _ in range(SAVE_TRIES):
        doc = await _editable_board(board_id, user_id)
        saved = await _try_save(doc, body)
        if saved is not None:
            return saved
    raise HTTPException(status_code=409, detail="board_busy")


async def _try_save(doc: dict, body: BoardPatch) -> Optional[BoardPatchResponse]:
    """One read-merge-write attempt; None when the stored revision moved."""
    before = await _content(doc)
    after = svc.merge_changes(before[0], before[1], body)
    svc.check_size(before, after)
    base_rev = int(doc.get("rev") or 0) if svc.is_v2(doc) else 1
    now = datetime.now(timezone.utc)
    fields: dict = {
        "schema": svc.SCHEMA, "rev": base_rev + 1,
        "nodes": after[0], "links": after[1], "updated_at": now,
    }
    if body.name is not None:
        fields["name"] = body.name
    if body.viewport is not None:
        fields["viewport"] = body.viewport.model_dump()
    elif not svc.is_v2(doc):
        fields["viewport"] = svc.viewport_of(doc)
    result = await db.blackboards.update_one(
        {"_id": doc["_id"], "rev": doc.get("rev"), "deleted_at": None},
        {"$set": fields, "$unset": {"edges": ""}},
    )
    if not result.matched_count:
        return None
    return BoardPatchResponse(rev=base_rev + 1, updated_at=now)


# ── PUT /{board_id} — gone: boards save by change ──────────────────────────
@router.put("/{board_id}", responses={410: {"description": "Gone"}})
async def save_blackboard(
    board_id: str, current_user: dict = Depends(get_firebase_user)
) -> None:
    raise HTTPException(status_code=410, detail=BOARDS_GONE)


# ── POST /{board_id}/duplicate ──────────────────────────────────────────────
@router.post("/{board_id}/duplicate", response_model=BoardResponse)
async def duplicate_board(
    board_id: str,
    current_user: dict = Depends(get_firebase_user),
    tier: str = Depends(get_subscription_tier),
) -> BoardResponse:
    """A copy owned by the caller, without collaborators. Counts against the
    caller's plan like a new board."""
    user_id = current_user.get("user_id")
    doc = await _editable_board(board_id, user_id)
    await _check_board_limit(user_id, tier)
    nodes, links = await _content(doc)
    name = svc.copy_name(doc.get("name") or "")
    copy = await _insert_board(user_id, name, nodes, links, svc.viewport_of(doc))
    return _board_response(copy)


# ── DELETE /{board_id} — the owner soft-deletes the board ──────────────────
@router.delete("/{board_id}", response_model=OkResponse)
async def delete_board(
    board_id: str, current_user: dict = Depends(get_firebase_user)
) -> dict:
    """Soft delete (SoftDeleteMixin's convention; the `soft_delete_ttl` index
    purges it after 30 days). A collaborator may edit a board, not delete it."""
    user_id = current_user.get("user_id")
    doc = await _find_board(board_id, user_id)
    if _owner_of(doc) != user_id:
        raise HTTPException(status_code=403, detail="only_owner_can_delete")
    now = datetime.now(timezone.utc)
    await db.blackboards.update_one(
        {"_id": doc["_id"]},
        {"$set": {"deleted_at": now, "deleted_by": user_id, "updated_at": now}},
    )
    return {"ok": True}


# ── Phase 7: PUT /{board_id}/invite — Add a collaborator ───────────────────
@router.put("/{board_id}/invite", response_model=OkResponse)
async def invite_collaborator(
    board_id: str,
    body: InviteCollaboratorRequest,
    current_user: dict = Depends(get_firebase_user),
) -> dict:
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
) -> BoardToCardResponse:
    user_id = current_user.get("user_id")

    # Access guard: owner or collaborator (T-07-02-02 mitigation)
    board = await _editable_board(board_id, user_id)

    # The texts the client read, else the stored nodes' own (a v2 note or
    # text gives its text, a frame its title, an item nothing).
    node_texts = body.node_texts or svc.selection_texts((await _content(board))[0], body.node_ids)

    # Cap at 20 nodes (T-07-02-03 mitigation)
    nodes_truncated = len(node_texts) > 20
    selected_texts = node_texts[:20]

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
