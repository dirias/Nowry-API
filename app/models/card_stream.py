# app/models/card_stream.py
"""SSE event payload models for the streaming card generation endpoint.

Wire contract (POST /card/generate/stream):
    event: card   -> CardEventData     (one per generated card)
    event: done   -> DoneEventData     (terminal success event)
    event: error  -> ErrorEventData    (terminal failure event)
    ": hb"        -> SSE comment heartbeat (keeps proxies from timing out)
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models.book_generation import GeneratedCard

StreamErrorCode = Literal[
    "AI_MALFORMED_OUTPUT",
    "AI_QUOTA_EXHAUSTED",
    "AI_PIPELINE_FAILED",
    "STREAM_TIMEOUT",
]

SSE_HEARTBEAT: str = ": hb\n\n"


class CardEventData(BaseModel):
    """Payload for a single `card` SSE event."""

    index: int
    total: int
    card: GeneratedCard


class DoneEventData(BaseModel):
    """Payload for the terminal `done` SSE event."""

    total_cards: int
    elapsed_ms: int
    # "auto" when the request omitted sampleNumber (adaptive), "fixed" otherwise.
    mode: Literal["auto", "fixed"]
    # Effective cap applied server-side (adaptive-derived or the fixed count).
    cap: int
    # True iff the raw model output exceeded `cap` and was clipped server-side.
    truncated: bool


class ErrorEventData(BaseModel):
    """Payload for the terminal `error` SSE event."""

    code: StreamErrorCode
    message: str


def sse_event(event_name: str, data: BaseModel) -> str:
    """Serialize a Pydantic payload into a single SSE event frame."""
    return f"event: {event_name}\ndata: {data.model_dump_json()}\n\n"
