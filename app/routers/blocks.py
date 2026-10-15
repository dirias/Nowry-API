"""`/blocks` — the Block and its log (PLAN-001, ADR-045, `docs/prd-blocks.md`).

Every route injects the Firebase user; every read is bounded; a block is
reachable only through its owner. The lazy migration (PRD D10) runs inside the
first GET for a user with no blocks, so the legacy routine and activities
arrive as blocks the moment the new Plan tab opens.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.firebase_auth import get_firebase_user
from app.config.database import block_log_collection, blocks_collection
from app.models.Block import (
    Block,
    BlockCreate,
    BlockLog,
    BlockLogUpsert,
    BlockSummary,
    BlockUpdate,
    BlocksResponse,
    DATE_PATTERN,
)
from app.models.common import OkResponse
from app.services.blocks import MAX_BLOCKS, MAX_LOGS, expand_occurrences, migrate_legacy, parse_range
from app.utils.logger import get_logger

router = APIRouter(prefix="/blocks", tags=["blocks"], dependencies=[Depends(get_firebase_user)])
logger = get_logger(__name__)

_DATE_RE = re.compile(DATE_PATTERN)


def _block_out(doc: dict) -> dict:
    out = {key: value for key, value in doc.items() if key not in ("_id", "deleted_at", "deleted_by")}
    out["id"] = str(doc["_id"])
    return out


def _log_out(doc: dict) -> dict:
    out = {key: value for key, value in doc.items() if key != "_id"}
    out["id"] = str(doc["_id"])
    return out


def _oid(block_id: str) -> ObjectId:
    if not ObjectId.is_valid(block_id):
        raise HTTPException(status_code=404, detail="Block not found")
    return ObjectId(block_id)


def _check_date(value: str) -> str:
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    return value


async def _active_blocks(user_id: str) -> list[dict]:
    return await blocks_collection.find({"user_id": user_id, "deleted_at": None}).to_list(length=MAX_BLOCKS)


async def _owned_block(user_id: str, block_id: str) -> dict:
    doc = await blocks_collection.find_one({"_id": _oid(block_id), "user_id": user_id, "deleted_at": None})
    if not doc:
        raise HTTPException(status_code=404, detail="Block not found")
    return doc


async def _logs_between(user_id: str, from_: str, to: str) -> list[dict]:
    return await block_log_collection.find(
        {"user_id": user_id, "date": {"$gte": from_, "$lte": to}}
    ).to_list(length=MAX_LOGS)


@router.get("", summary="Blocks and their occurrences for a range of local dates", response_model=BlocksResponse)
async def list_blocks(
    from_: str = Query(..., alias="from", description="first local date, YYYY-MM-DD"),
    to: str = Query(..., description="last local date, inclusive"),
    user: dict = Depends(get_firebase_user),
) -> dict:
    user_id = user.get("user_id")
    start, end = parse_range(from_, to)
    blocks = await _active_blocks(user_id)
    migrated: Optional[dict] = None
    if not blocks:
        report = await migrate_legacy(user_id)
        if report["blocks"]:
            migrated = report
            blocks = await _active_blocks(user_id)
    occurrences = expand_occurrences(blocks, start, end)
    by_key = {(log["block_id"], log["date"]): _log_out(log) for log in await _logs_between(user_id, from_, to)}
    for occurrence in occurrences:
        occurrence["log"] = by_key.get((occurrence["block_id"], occurrence["date"]))
    return {"blocks": [_block_out(b) for b in blocks], "occurrences": occurrences, "migrated": migrated}


@router.get("/summary", summary="Planned, kept, skipped and minutes over a range", response_model=BlockSummary)
async def block_summary(
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    user: dict = Depends(get_firebase_user),
) -> dict:
    user_id = user.get("user_id")
    start, end = parse_range(from_, to)
    blocks = await _active_blocks(user_id)
    by_id = {str(b["_id"]): b for b in blocks}
    summary = {"planned": len(expand_occurrences(blocks, start, end)), "kept": 0, "skipped": 0, "partial": 0, "minutes": 0}
    by_source: dict[str, int] = {}
    by_area: dict[str, int] = {}
    for log in await _logs_between(user_id, from_, to):
        summary[log["status"]] = summary.get(log["status"], 0) + 1
        if log["status"] == "skipped":
            continue
        minutes = int(log.get("minutes") or 0)
        summary["minutes"] += minutes
        block = by_id.get(log["block_id"]) or {}
        source = (block.get("source") or {}).get("type", "free")
        by_source[source] = by_source.get(source, 0) + minutes
        if block.get("area_id"):
            by_area[block["area_id"]] = by_area.get(block["area_id"], 0) + minutes
    return {**summary, "by_source": by_source, "by_area": by_area}


@router.post("", summary="Create a block", response_model=Block, status_code=201)
async def create_block(body: BlockCreate, user: dict = Depends(get_firebase_user)) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": ObjectId(),
        "user_id": user.get("user_id"),
        **body.model_dump(),
        "migrated_from": None,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "deleted_by": None,
    }
    await blocks_collection.insert_one(doc)
    return _block_out(doc)


def _merge_rule(doc: dict, changes: dict) -> dict:
    """A date clears the weekday rule and a weekday rule clears the date, unless
    the client sent both; the merged block must still have exactly one."""
    if changes.get("date") and "days_of_week" not in changes:
        changes["days_of_week"] = []
    if changes.get("days_of_week") and "date" not in changes:
        changes["date"] = None
    merged = {**doc, **changes}
    if bool(merged.get("days_of_week")) == bool(merged.get("date")):
        raise HTTPException(status_code=400, detail="a block repeats on days_of_week or happens once on a date")
    return merged


@router.patch("/{block_id}", summary="Change a block's fields", response_model=Block)
async def update_block(block_id: str, body: BlockUpdate, user: dict = Depends(get_firebase_user)) -> dict:
    user_id = user.get("user_id")
    doc = await _owned_block(user_id, block_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return _block_out(doc)
    merged = _merge_rule(doc, changes)
    changes["updated_at"] = datetime.now(timezone.utc)
    await blocks_collection.update_one({"_id": doc["_id"], "user_id": user_id}, {"$set": changes})
    return _block_out({**merged, "updated_at": changes["updated_at"]})


@router.delete("/{block_id}", summary="Remove a block (soft)", response_model=OkResponse)
async def delete_block(block_id: str, user: dict = Depends(get_firebase_user)) -> dict:
    user_id = user.get("user_id")
    now = datetime.now(timezone.utc)
    result = await blocks_collection.update_one(
        {"_id": _oid(block_id), "user_id": user_id, "deleted_at": None},
        {"$set": {"deleted_at": now, "deleted_by": user_id, "updated_at": now}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Block not found")
    return {"ok": True}


@router.put("/{block_id}/log/{date}", summary="Record a day's outcome for a block", response_model=BlockLog)
async def upsert_log(block_id: str, date: str, body: BlockLogUpsert, user: dict = Depends(get_firebase_user)) -> dict:
    user_id = user.get("user_id")
    await _owned_block(user_id, block_id)
    _check_date(date)
    key = {"user_id": user_id, "block_id": block_id, "date": date}
    now = datetime.now(timezone.utc)
    await block_log_collection.update_one(
        key,
        {"$set": {**body.model_dump(), "updated_at": now}, "$setOnInsert": {**key, "created_at": now}},
        upsert=True,
    )
    doc = await block_log_collection.find_one(key)
    return _log_out(doc)


@router.delete("/{block_id}/log/{date}", summary="Forget a day's outcome", response_model=OkResponse)
async def delete_log(block_id: str, date: str, user: dict = Depends(get_firebase_user)) -> dict:
    user_id = user.get("user_id")
    _oid(block_id)
    _check_date(date)
    await block_log_collection.delete_one({"user_id": user_id, "block_id": block_id, "date": date})
    return {"ok": True}
