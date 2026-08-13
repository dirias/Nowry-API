"""
AMagic TTS — POST /book/{book_id}/tts

Returns MP3 audio bytes from Google Cloud Text-to-Speech.
Tier gates: Free → 403; Plus → en-US only; Pro → language_code from request body.

Pro tier may additionally set `auto_detect: true` (ADR-001) to route text
through the shared `segment_text()` service (in-process Python import, no
HTTP) and synthesize one Google Cloud TTS call per detected-language segment,
stitched back together via `concatenate_mp3_segments()`. `auto_detect` is
silently ignored (never errored, never partially applied) for Plus/Free.
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
from app.models.tts import TextSegment, TTSRequest
from app.services.tts.audio_stitching import concatenate_mp3_segments
from app.services.tts.segmentation import segment_text
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

_TTS_SEGMENT_CAP_BYTES = 5000  # Safety cap: Google Cloud TTS max is 5000 UTF-8 bytes.
# Applied per segment in the auto-detect path (one synthesize_speech call per
# segment), and to the whole body in the single-call path (one call total) —
# see generate_tts(). A segment truncated mid-sentence by this cap is an
# accepted v1 edge case; _truncate_to_byte_limit() already handles it the same
# way the single-call path always has, no special-casing needed here.

# Auto-detect (Pro-only) fan-out cap: segment_text() can in principle return
# many small segments for pathologically fragmented/code-switched text: this
# caps provider spend fan-out per request. Remainder segments are truncated,
# not errored (docs/architecture.md "Known Risks & Mitigations").
_TTS_MAX_SEGMENTS_PER_REQUEST = 20

# Per-user request volume cap. This route can synthesise from any *public* book,
# not just the caller's own, so a Plus account could otherwise drive unbounded
# Google Cloud TTS spend (billed per character) over community content.
# _TTS_SEGMENT_CAP_BYTES bounds the cost of one segment/request; this bounds
# how many requests (regardless of how many segments each one fans out into —
# the rate limit counts 1 request no matter the segment count).
_TTS_RATE_LIMIT_MAX_REQUESTS = 60
_TTS_RATE_LIMIT_WINDOW_SECONDS = 3600
_TTS_RATE_LIMIT_DETAIL = "Too many audio requests. Please wait a moment."


def _derive_default_lang(language_code: str) -> str:
    """Derive an ISO 639-1 default language for segment_text() from a BCP-47 tag.

    `language_code` here is `body.language_code` (e.g. "en-US", "fr-FR"),
    but `segment_text()`'s `default_lang` parameter expects the shorter
    ISO 639-1 primary subtag (e.g. "en", "fr") — it's used as a fallback for
    script-neutral text and short, low-confidence Latin-script segments, not
    as a BCP-47 voice selector. Falls back to "en" for an empty/malformed tag.
    """
    primary_subtag = language_code.split("-", 1)[0].strip().lower()
    return primary_subtag or "en"


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

    # Auto-detect is a Pro-only path (ADR-001 "Related" tier-gating decision).
    # For any other tier the flag is silently ignored — never errored, never
    # partially applied — so Plus/Free behavior is byte-for-byte identical to
    # today regardless of what the caller sends for `auto_detect`.
    use_auto_detect: bool = body.auto_detect and tier == "pro"

    segments: list[TextSegment] = []
    text_input: str = ""
    input_char_count: int

    if use_auto_detect:
        # Segment the FULL, untruncated body — the per-segment byte cap is
        # applied below, per segment, not once on the whole body (see
        # _TTS_SEGMENT_CAP_BYTES).
        #
        # This sits in its own try/except, distinct from the main synthesis
        # try block below: a segmentation failure is a different failure
        # mode from a Google Cloud TTS failure, and the caller (TTSToolbar.js,
        # TASK-004) needs a `segmentation_failed:`-prefixed detail to show a
        # segmentation-specific error instead of the generic TTS one. Only
        # the server-side log gets the real exception text (D-style
        # convention matching the GoogleAuthError handler below) — the
        # client gets a fixed, safe message.
        try:
            default_lang = _derive_default_lang(language_code)
            segments = segment_text(body.text, default_lang=default_lang)[
                :_TTS_MAX_SEGMENTS_PER_REQUEST
            ]
            if not segments:
                # Empty/whitespace-only input: segment_text() returns []. Fall
                # back to a single segment carrying the raw text, so this hits
                # Google Cloud TTS (and the existing InvalidArgument -> 400
                # mapping below) the same way the single-call path always has,
                # rather than raising an unrelated stitching error.
                segments = [TextSegment(text=body.text, lang_code=default_lang)]
        except Exception as exc:
            logger.exception(
                f"[tts] segmentation failed for book={book_id} tier={tier}: {exc}"
            )
            raise HTTPException(
                status_code=500,
                detail="segmentation_failed: unable to process this text for auto-detect playback",
            )
        input_char_count = sum(len(segment.text) for segment in segments)
    else:
        # Sanitize input — plain text only, no SSML (T-6-03 mitigation).
        # Truncated by UTF-8 byte count, not character count: Google Cloud TTS's
        # 5000-byte limit applies to the encoded text, and non-ASCII text can
        # exceed 5000 bytes well before 5000 characters.
        text_input = _truncate_to_byte_limit(body.text, _TTS_SEGMENT_CAP_BYTES)
        input_char_count = len(text_input)

    client = get_langfuse_client()
    trace_metadata = {
        "feature": "tts_amagic",
        "tier": tier,
        "user_id": user_id,
        "language_code": language_code,
        "input_char_count": input_char_count,
        # Attribute non-owner usage: is_owner=False with is_public=True is the
        # community-browse path, and owner_id identifies whose public book is
        # driving the spend.
        "is_public": is_public_book,
        "owner_id": owner_id,
        "is_owner": owner_id == user_id,
        # Lets cost/usage dashboards distinguish auto-detect (N-segment,
        # N-call) requests from manual (single-call) ones.
        "segment_count": len(segments) if use_auto_detect else 1,
        "auto_detect": use_auto_detect,
    }

    try:
        tts_client = get_tts_client()
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        )

        if not use_auto_detect:
            synthesis_input = texttospeech.SynthesisInput(text=text_input)
            voice = texttospeech.VoiceSelectionParams(
                language_code=language_code,
                ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
            )

        # Set up Langfuse tracing context BEFORE the synthesis call(s)
        # (fire-and-forget, TR-06). If construction fails, fall back to a
        # no-op context manager — each synthesis call below still executes
        # exactly once, never retried for tracing reasons (once total for the
        # single-call path, once per segment for the auto-detect path).
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
                    input={"text_char_count": input_char_count, "language_code": language_code},
                )
            except Exception as langfuse_exc:
                logger.warning(
                    f"[tts] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                )
                attrs_cm = contextlib.nullcontext()
                span_cm = contextlib.nullcontext()

        with attrs_cm, span_cm as span:
            start = time.monotonic()

            if use_auto_detect:
                # ── Per-segment TTS synthesis — executes once per segment,
                # never retried per segment. One child span per segment,
                # nested under the parent "tts_amagic" span above, so
                # existing per-request dashboards keep their shape while
                # gaining per-segment detail. ──────────────────────────────
                audio_chunks: list[bytes] = []
                for segment in segments:
                    segment_text_input = _truncate_to_byte_limit(
                        segment.text, _TTS_SEGMENT_CAP_BYTES
                    )
                    segment_synthesis_input = texttospeech.SynthesisInput(
                        text=segment_text_input
                    )
                    segment_voice = texttospeech.VoiceSelectionParams(
                        language_code=segment.lang_code,
                        ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL,
                    )

                    segment_span_cm = contextlib.nullcontext()
                    if client and span is not None:
                        try:
                            segment_span_cm = client.start_as_current_observation(
                                name="tts_amagic_segment",
                                as_type="span",
                                input={
                                    "lang_code": segment.lang_code,
                                    "char_count": len(segment.text),
                                },
                            )
                        except Exception as langfuse_exc:
                            logger.warning(
                                f"[tts] Langfuse segment span failed, continuing "
                                f"without it: {langfuse_exc}"
                            )
                            segment_span_cm = contextlib.nullcontext()

                    with segment_span_cm as segment_span:
                        segment_response = tts_client.synthesize_speech(
                            input=segment_synthesis_input,
                            voice=segment_voice,
                            audio_config=audio_config,
                        )
                        if segment_span is not None:
                            try:
                                segment_span.update(
                                    output={
                                        "audio_byte_size": len(
                                            segment_response.audio_content
                                        ),
                                    },
                                )
                            except Exception as langfuse_exc:
                                logger.warning(
                                    f"[tts] Langfuse segment span failed, continuing "
                                    f"without it: {langfuse_exc}"
                                )
                    audio_chunks.append(segment_response.audio_content)

                audio_bytes = concatenate_mp3_segments(audio_chunks)
            else:
                # ── The ONE TTS synthesis call — executes exactly once ────
                tts_response = tts_client.synthesize_speech(
                    input=synthesis_input, voice=voice, audio_config=audio_config,
                )
                audio_bytes = tts_response.audio_content

            latency_ms = (time.monotonic() - start) * 1000

            # Record the result on the Langfuse span, if one is active. A failure
            # here is a tracing-only failure: the audio is already in hand, so we
            # log and continue — never re-invoke synthesize_speech.
            if span is not None:
                try:
                    # D-13: no raw audio bytes in trace — byte SIZE only
                    span.update(
                        output={
                            "audio_byte_size": len(audio_bytes),
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
            content=audio_bytes,
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
    except ValueError as exc:
        # concatenate_mp3_segments() raises ValueError on empty input (its own
        # docstring). `segments` is guaranteed non-empty above, so this
        # shouldn't happen in practice, but if it does it's a segmentation/
        # stitching failure, not a generic synthesis failure — give the
        # caller the same distinguishable `segmentation_failed:` prefix as
        # the segmentation try/except above, not the generic catch-all
        # message. Only reachable via the auto-detect path (the only place
        # ValueError can originate from in this try block).
        logger.exception(
            f"[tts] audio stitching failed for book={book_id} tier={tier}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail="segmentation_failed: unable to combine synthesized audio segments",
        )
    except Exception as exc:
        logger.exception(f"[tts] synthesis failed for book={book_id} tier={tier}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Audio generation failed. Please try again.",
        )
