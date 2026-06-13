"""
AMagic TTS — POST /book/{book_id}/tts

Returns MP3 audio bytes from Google Cloud Text-to-Speech.
Tier gates: Free → 403; Plus → en-US only; Pro → language_code from request body.
"""
from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from google.cloud import texttospeech

from app.ai_orchestrator.llm_clients.tts_client import get_tts_client
from app.auth.dependencies import get_subscription_tier
from app.auth.firebase_auth import get_firebase_user
from app.config.database import books_collection
from app.models.tts import TTSRequest
from app.utils.logger import get_logger
from app.core.langfuse_client import get_langfuse_client
from langfuse import propagate_attributes
import contextlib
import time

logger = get_logger(__name__)

router = APIRouter(
    prefix="/book",
    tags=["tts"],
    dependencies=[Depends(get_firebase_user)],
)

_TTS_TEXT_CAP = 5000  # Safety cap: Google Cloud TTS max is 5000 bytes


@router.post("/{book_id}/tts")
async def generate_tts(
    book_id: str,
    body: TTSRequest,
    tier: str = Depends(get_subscription_tier),
    current_user: dict = Depends(get_firebase_user),
) -> Response:
    """Generate TTS audio for a book section or full book text.

    Plus: en-US only. Pro: user-selected language. Free: 403.
    """
    if tier == "free":
        raise HTTPException(
            status_code=403,
            detail="TTS requires a Plus or Pro subscription.",
        )

    user_id: str = current_user.get("user_id", "")

    # Ownership check (T-6-04)
    try:
        book = await books_collection.find_one(
            {"_id": ObjectId(book_id), "deleted_at": None}
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")

    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    # Pro-only language override; Plus always uses en-US (T-6-02 mitigation)
    language_code: str = body.language_code if tier == "pro" else "en-US"

    # Sanitize input — plain text only, no SSML (T-6-03 mitigation)
    text_input: str = body.text[:_TTS_TEXT_CAP]

    client = get_langfuse_client()
    trace_metadata = {
        "feature": "tts_amagic",
        "tier": tier,
        "user_id": user_id,
        "language_code": language_code,
        "input_char_count": len(text_input),
    }

    try:
        tts_client = get_tts_client()
        synthesis_input = texttospeech.SynthesisInput(text=text_input)
        voice = texttospeech.VoiceSelectionParams(
            language_code=language_code,
            ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        )

        # Set up Langfuse tracing context BEFORE the synthesis call (fire-and-forget,
        # TR-06). If construction fails, fall back to a no-op context manager — the
        # synthesis call below executes exactly once either way, never retried for
        # tracing reasons.
        attrs_cm = contextlib.nullcontext()
        span_cm = contextlib.nullcontext()
        if client:
            try:
                attrs_cm = propagate_attributes(
                    user_id=user_id,
                    trace_name="tts_amagic",
                    metadata=trace_metadata,
                    tags=["tts_amagic", tier],
                )
                span_cm = client.start_as_current_observation(
                    name="tts_amagic",
                    as_type="span",  # never a generation/observe-decorator type (D-06)
                    input={"text_char_count": len(text_input), "language_code": language_code},
                )
            except Exception as langfuse_exc:
                logger.warning(
                    f"[tts] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                )
                attrs_cm = contextlib.nullcontext()
                span_cm = contextlib.nullcontext()

        with attrs_cm, span_cm as span:
            start = time.monotonic()
            # ── The ONE TTS synthesis call — executes exactly once ──────────────
            tts_response = tts_client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config,
            )
            latency_ms = (time.monotonic() - start) * 1000

            # Record the result on the Langfuse span, if one is active. A failure
            # here is a tracing-only failure: the audio is already in hand, so we
            # log and continue — never re-invoke synthesize_speech.
            if span is not None:
                try:
                    # D-13: no raw audio bytes in trace — byte SIZE only
                    span.update(
                        output={
                            "audio_byte_size": len(tts_response.audio_content),
                            "voice_ssml_gender": "NEUTRAL",
                            "latency_ms": round(latency_ms, 1),
                        },
                        metadata={**trace_metadata, "voice_name": language_code},  # D-12
                    )
                except Exception as langfuse_exc:
                    logger.warning(
                        f"[tts] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                    )

        return Response(
            content=tts_response.audio_content,
            media_type="audio/mpeg",
        )
    except Exception as exc:
        logger.error(f"[tts] synthesis failed for book={book_id} tier={tier}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
