"""
TASK-002 — POST /book/{book_id}/tts auto-detect (Pro-only), using the shared
`segment_text()` service.

Covers:
1. auto_detect=true + Pro returns stitched, correctly-ordered audio spanning
   all detected segments.
2. auto_detect=true + Plus behaves identically to auto_detect=false + Plus
   (still en-US, no segmentation, exactly one synthesize_speech call).
3. auto_detect=true + Free still 403s before reaching any of this logic.
4. A >20-segment input is truncated to 20, not errored.
5. Existing non-auto-detect requests are byte-for-byte unchanged (regression
   against the pre-TASK-002 single-call path).
6. Langfuse trace: one parent span ("tts_amagic") with N child spans
   ("tts_amagic_segment") for an N-segment auto-detect request, and
   trace_metadata carries segment_count/auto_detect.

Reuses the same `sys.modules` guard as test_tts_public_access.py: this file
must import the REAL app.routers.tts, not a MagicMock stub some other test
module may have registered first.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId

if isinstance(sys.modules.get("app.routers.tts"), MagicMock):
    del sys.modules["app.routers.tts"]

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import app.routers.tts as tts_module  # noqa: E402
from app.models.tts import TTSRequest  # noqa: E402
from app.services.tts.segmentation import segment_text as real_segment_text  # noqa: E402

OWNER_ID = "507f1f77bcf86cd799439011"
BOOK_ID = ObjectId("60b8d295f1d2c17f4e4b1111")


class FakeBooksCollection:
    """Minimal Motor stand-in: one owned, non-public, non-deleted book."""

    def __init__(self) -> None:
        self.doc = {
            "_id": BOOK_ID,
            "user_id": OWNER_ID,
            "is_public": False,
            "deleted_at": None,
        }

    async def find_one(self, query: dict, projection=None):
        if query.get("_id") != BOOK_ID:
            return None
        return dict(self.doc)


def _tts_client_with_lang_keyed_audio(call_order: list) -> MagicMock:
    """Fake Google TTS client whose output bytes depend on voice.language_code.

    Also appends every requested language_code to `call_order`, in call
    order, so tests can assert both content and ordering without depending
    on implementation internals.
    """
    client = MagicMock()

    def _synthesize(input, voice, audio_config):  # noqa: A002 - matches SDK kwarg name
        lang = voice.language_code
        call_order.append(lang)
        return MagicMock(audio_content=f"audio[{lang}]".encode())

    client.synthesize_speech.side_effect = _synthesize
    return client


def _patched_router(fake_books, fake_tts_client, langfuse_client=None):
    return (
        patch.object(tts_module, "books_collection", fake_books),
        patch.object(tts_module, "get_tts_client", return_value=fake_tts_client),
        patch.object(tts_module, "get_langfuse_client", return_value=langfuse_client),
        patch.object(tts_module, "enforce_user_rate_limit", new=AsyncMock(return_value=1)),
    )


async def _call(*, tier: str, auto_detect: bool, text: str, language_code: str = "en-US"):
    request_kwargs = {"text": text, "language_code": language_code}
    if auto_detect:
        request_kwargs["auto_detect"] = True
    return await tts_module.generate_tts(
        book_id=str(BOOK_ID),
        body=TTSRequest(**request_kwargs),
        tier=tier,
        current_user={"user_id": OWNER_ID},
    )


# ---------------------------------------------------------------------------
# Acceptance 1 — auto_detect=true + Pro -> stitched, correctly-ordered audio
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auto_detect_pro_returns_stitched_ordered_audio():
    text = "Hi there. こんにちは。"
    expected_segments = real_segment_text(text, default_lang="en")
    assert [s.lang_code for s in expected_segments] == ["en", "ja"]

    call_order: list = []
    fake_tts_client = _tts_client_with_lang_keyed_audio(call_order)
    fake_books = FakeBooksCollection()

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        response = await _call(tier="pro", auto_detect=True, text=text)

    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"
    assert call_order == ["en", "ja"]
    assert response.body == b"audio[en]" + b"audio[ja]"
    assert fake_tts_client.synthesize_speech.call_count == 2


# ---------------------------------------------------------------------------
# Acceptance 2 — auto_detect=true + Plus is identical to auto_detect=false + Plus
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auto_detect_plus_behaves_like_auto_detect_false():
    text = "Bonjour le monde. こんにちは。"
    mock_texttospeech = MagicMock()
    fake_books = FakeBooksCollection()

    fake_tts_client_a = MagicMock()
    fake_tts_client_a.synthesize_speech.return_value = MagicMock(audio_content=b"plus-audio")
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client_a)
    with p1, p2, p3, p4, patch.object(tts_module, "texttospeech", mock_texttospeech):
        response_auto = await _call(tier="plus", auto_detect=True, text=text, language_code="fr-FR")
        auto_kwargs = mock_texttospeech.VoiceSelectionParams.call_args.kwargs

    fake_tts_client_b = MagicMock()
    fake_tts_client_b.synthesize_speech.return_value = MagicMock(audio_content=b"plus-audio")
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client_b)
    with p1, p2, p3, p4, patch.object(tts_module, "texttospeech", mock_texttospeech):
        response_manual = await _call(tier="plus", auto_detect=False, text=text, language_code="fr-FR")
        manual_kwargs = mock_texttospeech.VoiceSelectionParams.call_args.kwargs

    # Plus always clamps to en-US regardless of requested language_code, and
    # auto_detect must be silently ignored — both paths take exactly one
    # synthesize_speech call with an identical voice selection.
    assert auto_kwargs["language_code"] == "en-US"
    assert manual_kwargs["language_code"] == "en-US"
    assert fake_tts_client_a.synthesize_speech.call_count == 1
    assert fake_tts_client_b.synthesize_speech.call_count == 1
    assert response_auto.body == response_manual.body == b"plus-audio"


@pytest.mark.asyncio
async def test_auto_detect_plus_trace_metadata_matches_manual():
    """segment_count/auto_detect metadata must reflect the ignored flag, not the request."""
    fake_books = FakeBooksCollection()
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"x")
    langfuse_client = MagicMock()

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client, langfuse_client)
    with p1, p2, p3, p4, patch.object(tts_module, "propagate_attributes") as mock_propagate:
        await _call(tier="plus", auto_detect=True, text="Hello world.")

    metadata = mock_propagate.call_args.kwargs["metadata"]
    assert metadata["auto_detect"] is False
    assert metadata["segment_count"] == 1


# ---------------------------------------------------------------------------
# Acceptance 3 — auto_detect=true + Free still 403s before any of this logic
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auto_detect_free_still_403():
    fake_books = FakeBooksCollection()
    fake_tts_client = MagicMock()
    rate_limit = AsyncMock(return_value=1)
    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, patch.object(tts_module, "enforce_user_rate_limit", new=rate_limit):
        with pytest.raises(HTTPException) as exc_info:
            await _call(tier="free", auto_detect=True, text="Hello")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "TTS requires a Plus or Pro subscription."
    assert fake_tts_client.synthesize_speech.call_count == 0
    rate_limit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Acceptance 4 — segment cap: >20 segments truncated, not errored
# ---------------------------------------------------------------------------
_SCRIPT_SENTENCES = [
    "Hello there.",       # latin -> en
    "你好世界.",             # han (no kana) -> zh
    "안녕하세요.",             # hangul -> ko
    "Привет мир.",         # cyrillic -> ru
    "مرحبا بالعالم.",       # arabic -> ar
    "नमस्ते दुनिया.",         # devanagari -> hi
]


@pytest.mark.asyncio
async def test_over_20_segments_truncated_to_20_not_errored():
    # 6 distinct scripts x 4 repeats = 24 sentences; adjacent sentences never
    # share a script (and therefore never share a lang_code), so none merge.
    text = " ".join(_SCRIPT_SENTENCES * 4)
    raw_segments = real_segment_text(text, default_lang="en")
    assert len(raw_segments) > 20, "fixture must exceed the cap for this test to be meaningful"

    expected_first_20_langs = [s.lang_code for s in raw_segments[:20]]

    call_order: list = []
    fake_tts_client = _tts_client_with_lang_keyed_audio(call_order)
    fake_books = FakeBooksCollection()

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        response = await _call(tier="pro", auto_detect=True, text=text)

    assert response.status_code == 200
    assert fake_tts_client.synthesize_speech.call_count == 20
    assert call_order == expected_first_20_langs
    assert tts_module._TTS_MAX_SEGMENTS_PER_REQUEST == 20


# ---------------------------------------------------------------------------
# Acceptance 5 — existing non-auto-detect requests are byte-for-byte unchanged
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_auto_detect_request_unchanged_single_call_path():
    """Regression: a request with no `auto_detect` field at all (the shape every
    caller sent before TASK-002) must still take exactly the old single-call
    path — one synthesize_speech call, response body equal to that call's
    audio_content untouched by any stitching.
    """
    fake_books = FakeBooksCollection()
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"unchanged-mp3-bytes")

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        # Body constructed the same way pre-TASK-002 callers always have —
        # no `auto_detect` key at all, relying on the Pydantic default.
        response = await tts_module.generate_tts(
            book_id=str(BOOK_ID),
            body=TTSRequest(text="Some book text.", language_code="fr-FR"),
            tier="pro",
            current_user={"user_id": OWNER_ID},
        )

    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"
    assert response.body == b"unchanged-mp3-bytes"
    assert fake_tts_client.synthesize_speech.call_count == 1


@pytest.mark.asyncio
async def test_default_auto_detect_field_is_false():
    assert TTSRequest(text="x").auto_detect is False


# ---------------------------------------------------------------------------
# Acceptance 6 — Langfuse: one parent span, N child spans, metadata fields
# ---------------------------------------------------------------------------
class _FakeSpan:
    def __init__(self) -> None:
        self.update_calls: list = []

    def update(self, **kwargs):
        self.update_calls.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.observation_calls: list = []

    def start_as_current_observation(self, **kwargs):
        self.observation_calls.append(kwargs)
        return _FakeSpan()


@pytest.mark.asyncio
async def test_langfuse_one_parent_span_n_child_spans_for_auto_detect():
    text = "Hi there. こんにちは。"
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"a")
    fake_books = FakeBooksCollection()
    langfuse_client = _FakeLangfuseClient()

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client, langfuse_client)
    with p1, p2, p3, p4, patch.object(tts_module, "propagate_attributes"):
        await _call(tier="pro", auto_detect=True, text=text)

    calls = langfuse_client.observation_calls
    assert len(calls) == 3  # 1 parent + 2 segment children
    assert calls[0]["name"] == "tts_amagic"
    assert calls[0]["as_type"] == "span"
    assert calls[1]["name"] == "tts_amagic_segment"
    assert calls[1]["as_type"] == "span"
    assert calls[1]["input"]["lang_code"] == "en"
    assert calls[2]["name"] == "tts_amagic_segment"
    assert calls[2]["input"]["lang_code"] == "ja"


@pytest.mark.asyncio
async def test_langfuse_trace_metadata_carries_segment_count_and_auto_detect():
    text = "Hi there. こんにちは。"
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"a")
    fake_books = FakeBooksCollection()
    langfuse_client = _FakeLangfuseClient()

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client, langfuse_client)
    with p1, p2, p3, p4, patch.object(tts_module, "propagate_attributes") as mock_propagate:
        await _call(tier="pro", auto_detect=True, text=text)

    metadata = mock_propagate.call_args.kwargs["metadata"]
    assert metadata["segment_count"] == 2
    assert metadata["auto_detect"] is True


@pytest.mark.asyncio
async def test_langfuse_single_parent_span_only_for_non_auto_detect():
    """Unchanged shape check: the non-auto-detect path must not gain child spans."""
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"a")
    fake_books = FakeBooksCollection()
    langfuse_client = _FakeLangfuseClient()

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client, langfuse_client)
    with p1, p2, p3, p4, patch.object(tts_module, "propagate_attributes"):
        await _call(tier="pro", auto_detect=False, text="Hello world.")

    calls = langfuse_client.observation_calls
    assert len(calls) == 1
    assert calls[0]["name"] == "tts_amagic"


# ---------------------------------------------------------------------------
# TASK-005 — distinguishable error contract for auto-detect segmentation/
# stitching failures. `segmentation_failed:`-prefixed detail must reach the
# caller (matches nowry/src/components/Books/TTSToolbar.js's
# `error.ttsDetail?.startsWith('segmentation_failed:')` check from TASK-004),
# and non-segmentation failures must keep their existing exact messages.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_segment_text_failure_returns_segmentation_failed_500():
    fake_books = FakeBooksCollection()
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"a")

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4, patch.object(
        tts_module, "segment_text", side_effect=RuntimeError("classifier exploded")
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _call(tier="pro", auto_detect=True, text="Hi there. こんにちは。")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail.startswith("segmentation_failed:")
    fake_tts_client.synthesize_speech.assert_not_called()


@pytest.mark.asyncio
async def test_concatenate_mp3_segments_value_error_returns_segmentation_failed_500():
    fake_books = FakeBooksCollection()
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.return_value = MagicMock(audio_content=b"a")

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4, patch.object(
        tts_module,
        "concatenate_mp3_segments",
        side_effect=ValueError("no audio chunks to concatenate"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _call(tier="pro", auto_detect=True, text="Hi there. こんにちは。")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail.startswith("segmentation_failed:")


@pytest.mark.asyncio
async def test_non_segmentation_google_api_failure_keeps_existing_message():
    """Regression: a non-segmentation Pro-tier synthesis failure (Google API
    InvalidArgument) must still return the existing 400 with the existing
    unchanged detail — proving the new `except ValueError` clause did not
    accidentally widen its scope to catch unrelated failures.
    """
    fake_books = FakeBooksCollection()
    fake_tts_client = MagicMock()
    fake_tts_client.synthesize_speech.side_effect = (
        tts_module.google_api_exceptions.InvalidArgument("bad request")
    )

    p1, p2, p3, p4 = _patched_router(fake_books, fake_tts_client)
    with p1, p2, p3, p4:
        with pytest.raises(HTTPException) as exc_info:
            await _call(tier="pro", auto_detect=False, text="Hello world.")

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "This section could not be converted to audio. Try a shorter section."
    )
