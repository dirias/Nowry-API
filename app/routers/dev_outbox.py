"""
`GET /dev/outbox` — the emails the API rendered, for the dev end-to-end suite (ADR-090).

Mounted only when `app.services.dev_outbox.should_mount()` says so, which is
never in production. Every request also needs `X-Dev-Outbox-Key` equal to
`DEV_OUTBOX_KEY`; anything else, including a missing key on the server, is a
plain 404, as if the route did not exist.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.services import dev_outbox

router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)


class OutboxResponse(BaseModel):
    items: List[Dict[str, Any]]


def _allowed(key: Optional[str]) -> bool:
    return dev_outbox.valid_key(key)


@router.get("/outbox", response_model=OutboxResponse)
async def read_outbox(
    to: Optional[str] = Query(default=None, max_length=254),
    kind: Optional[str] = Query(default=None, max_length=40),
    limit: int = Query(default=20, ge=1, le=dev_outbox.READ_LIMIT),
    x_dev_outbox_key: Optional[str] = Header(default=None),
) -> OutboxResponse:
    """The newest emails first, filtered by address and kind."""
    if not _allowed(x_dev_outbox_key):
        raise HTTPException(status_code=404, detail="Not Found")
    return OutboxResponse(items=await dev_outbox.read(to, kind, limit))
