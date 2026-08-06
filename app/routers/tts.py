"""
AMagic TTS — POST /book/{book_id}/tts

Returns MP3 audio bytes from Google Cloud Text-to-Speech.
Tier gates: Free → 403; Plus → en-US only; Pro → language_code from request body.
"""
from __future__ import annotations

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from google.api_core import exceptions as google_api_exceptions
from google.auth import exceptions as google_auth_exceptions
from google.cloud import texttospeech

from app.ai_orchestrator.llm_clients.tts_client import get_tts_client
from app.auth.dependencies import get_subscription_tier
from app.auth.firebase_auth import get_firebase_user
from app.config.database import books_collection
from app.models.tts import TTSRequest
from app.utils.logger import get_logger
from app.utils.rate_limit import enforce_user_rate_limit
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

_TTS_TEXT_CAP_BYTES = 5000  # Safety cap: Google Cloud TTS max is 5000 UTF-8 bytes

# Per-user request volume cap. This route can synthesise from any *public* book,
# not just the caller's own, so a Plus account could otherwise drive unbounded
# Google Cloud TTS spend (billed per character) over community content.
# _TTS_TEXT_CAP_BYTES bounds the cost of one request; this bounds how many.
_TTS_RATE_LIMIT_MAX_REQUESTS = 60
_TTS_RATE_LIMIT_WINDOW_SECONDS = 3600
_TTS_RATE_LIMIT_DETAIL = "Too many audio requests. Please wait a moment."


def _truncate_to_byte_limit(text: str, max_bytes: int) -> str:
    """Truncate text to at most `max_bytes` when UTF-8 encoded.

    Slicing by character count (e.g. text[:5000]) can still exceed the byte
    limit for non-ASCII text (accents, CJK, emoji), which Google Cloud TTS
    rejects with an InvalidArgument error. Truncating on the encoded bytes
    and decoding with errors="ignore" guarantees the result fits.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


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

    # Validate the book_id format first — malformed IDs are a client error (400).
    try:
        book_object_id = ObjectId(book_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")

    # Access check (T-6-04). The caller may read a book they own OR any book
    # published to the community browse surface (is_public), so that "Listen"
    # works on public content before it is forked — a fork already produces a
    # copy owned by the forker, so the pre-fork read was the only gap.
    # Mirrors require_public_or_ownership() in app/auth/dependencies.py.
    #
    # The predicate lives in the query, not in Python, so a private book owned
    # by someone else is never loaded into this process at all. deleted_at:None
    # still applies to BOTH branches — a soft-deleted public book stays a 404.
    #
    # Projected down to the access-control and tracing fields: the caller sends
    # its own text, so nothing here needs `full_content`, which is the entire
    # body of the book and is now fetchable for public books too.
    #
    # A failure here (e.g. a transient Mongo error) is a server-side problem,
    # not a malformed ID — log it with a traceback so it's diagnosable in
    # Railway logs instead of being silently misreported.
    try:
        book = await books_collection.find_one(
            {
                "_id": book_object_id,
                "deleted_at": None,
                "$or": [{"user_id": user_id}, {"is_public": True}],
            },
            {"user_id": 1, "is_public": 1},
        )
    except Exception as exc:
        logger.exception(f"[tts] book lookup failed for book_id={book_id}: {exc}")
        raise HTTPException(
            status_code=500, detail="Unable to load book. Please try again."
        )

    # Deliberately 404, never 403: a 403 would confirm to a stranger that a
    # given private book exists. Missing, soft-deleted, and inaccessible are
    # indistinguishable to the caller by design — do not "improve" this.
    if not book:
        raise HTTPException(status_code=404, detail="Book not found.")

    owner_id: str = str(book.get("user_id") or "")
    is_public_book: bool = bool(book.get("is_public", False))

    # Volume cap, applied once the request is known to be authorised and about
    # to reach the paid provider. Raises 429.
    await enforce_user_rate_limit(
        user_id=user_id,
        feature="tts_amagic",
        limit=_TTS_RATE_LIMIT_MAX_REQUESTS,
        window_seconds=_TTS_RATE_LIMIT_WINDOW_SECONDS,
        detail=_TTS_RATE_LIMIT_DETAIL,
    )

    # Pro-only language override; Plus always uses en-US (T-6-02 mitigation)
    language_code: str = body.language_code if tier == "pro" else "en-US"

    # Sanitize input — plain text only, no SSML (T-6-03 mitigation).
    # Truncated by UTF-8 byte count, not character count: Google Cloud TTS's
    # 5000-byte limit applies to the encoded text, and non-ASCII text can
    # exceed 5000 bytes well before 5000 characters.
    text_input: str = _truncate_to_byte_limit(body.text, _TTS_TEXT_CAP_BYTES)

    client = get_langfuse_client()
    trace_metadata = {
        "feature": "tts_amagic",
        "tier": tier,
        "user_id": user_id,
        "language_code": language_code,
        "input_char_count": len(text_input),
        # Attribute non-owner usage: is_owner=False with is_public=True is the
        # community-browse path, and owner_id identifies whose public book is
        # driving the spend.
        "is_public": is_public_book,
        "owner_id": owner_id,
        "is_owner": owner_id == user_id,
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
    except google_auth_exceptions.GoogleAuthError as exc:
        # Missing/invalid GOOGLE_TTS_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS
        # (no Application Default Credentials available in this environment).
        # logger.exception captures the full traceback for Railway logs; the
        # client only gets a generic message — the credentials detail is not
        # something the caller can act on and shouldn't be exposed.
        logger.exception(
            f"[tts] Google credentials misconfigured for book={book_id} tier={tier}: {exc}"
        )
        raise HTTPException(
            status_code=503,
            detail="TTS service is temporarily unavailable. Please try again later.",
        )
    except google_api_exceptions.InvalidArgument as exc:
        # e.g. text still rejected by Google despite our byte-length cap.
        logger.exception(
            f"[tts] Google TTS rejected input for book={book_id} tier={tier}: {exc}"
        )
        raise HTTPException(
            status_code=400,
            detail="This section could not be converted to audio. Try a shorter section.",
        )
    except google_api_exceptions.GoogleAPICallError as exc:
        logger.exception(
            f"[tts] Google TTS API call failed for book={book_id} tier={tier}: {exc}"
        )
        raise HTTPException(
            status_code=502,
            detail="TTS service is temporarily unavailable. Please try again later.",
        )
    except Exception as exc:
        logger.exception(f"[tts] synthesis failed for book={book_id} tier={tier}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Audio generation failed. Please try again.",
        )
