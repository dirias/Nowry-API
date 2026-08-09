"""Language segmentation — POST /v1/tts/segment

Splits caller-supplied text into per-language segments via the shared
`segment_text()` service (ADR-001, `app/services/tts/segmentation.py`), so
the Study Cards frontend can play each segment with a voice matching its own
detected language (`nowry/src/hooks/useSegmentedSpeech.js`).

This is the *only* network boundary `segment_text()` crosses — the Books
"Listen" path (`app/routers/tts.py`) calls it directly in-process, same
Python runtime, no HTTP hop (see ADR-001).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from app.auth.firebase_auth import get_firebase_user
from app.models.tts import SegmentRequest, SegmentResponse
from app.services.tts.segmentation import segment_text
from app.utils.logger import get_logger
from app.utils.rate_limit import enforce_user_rate_limit

logger = get_logger(__name__)

router = APIRouter(
    prefix="/v1/tts",
    tags=["tts"],
    dependencies=[Depends(get_firebase_user)],
)

# Separate quota namespace from "tts_amagic" (app/routers/tts.py). This route
# is cheap NLP with no Google Cloud billing behind it and its primary caller
# is the free Study Cards surface, so it must never compete with, or be
# throttled alongside, the paid Books TTS quota.
_SEGMENT_RATE_LIMIT_FEATURE = "tts_segment"
_SEGMENT_RATE_LIMIT_MAX_REQUESTS = 300
_SEGMENT_RATE_LIMIT_WINDOW_SECONDS = 3600
_SEGMENT_RATE_LIMIT_DETAIL = "Too many segmentation requests. Please wait a moment."

_INVALID_TEXT_DETAIL = "Text must be non-empty and at most 10000 characters."


@router.post(
    "/segment",
    response_model=SegmentResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": SegmentRequest.model_json_schema()}
            },
        }
    },
)
async def segment_tts_text(
    request: Request,
    current_user: dict = Depends(get_firebase_user),
) -> SegmentResponse:
    """Split request text into per-language segments.

    NOT tier-gated — every authenticated user gets this (unlike
    `/book/{book_id}/tts`, which requires Plus/Pro), metered only by its own
    300/hr rate limit, independent of the `tts_amagic` quota.

    The body is parsed and validated manually against `SegmentRequest`
    (rather than via a typed Pydantic body parameter) so this route can
    return `400` for both empty and oversized text — matching the documented
    API contract (`docs/architecture.md` API Contracts) — instead of
    FastAPI's default `422` for a request-body schema violation.
    """
    user_id: str = current_user.get("user_id", "")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail=_INVALID_TEXT_DETAIL)

    try:
        body = SegmentRequest.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=400, detail=_INVALID_TEXT_DETAIL)

    text: str = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail=_INVALID_TEXT_DETAIL)

    # Quota check happens only after the request is known to be well-formed,
    # so a malformed (400) request never burns a user's 300/hr allowance.
    await enforce_user_rate_limit(
        user_id=user_id,
        feature=_SEGMENT_RATE_LIMIT_FEATURE,
        limit=_SEGMENT_RATE_LIMIT_MAX_REQUESTS,
        window_seconds=_SEGMENT_RATE_LIMIT_WINDOW_SECONDS,
        detail=_SEGMENT_RATE_LIMIT_DETAIL,
    )

    try:
        segments = segment_text(text)
    except Exception as exc:
        # Full traceback logged server-side only — classifier internals
        # (lingua, script-range regexes) are never leaked to the client.
        logger.exception(
            f"[tts_segment] segmentation failed for user_id={user_id}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail="Segmentation failed. Please try again.",
        )

    return SegmentResponse(segments=segments)
