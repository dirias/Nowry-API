"""TASK-001 — canonical language segmentation service + POST /v1/tts/segment.

Covers:
  - `segment_text()` (pure function, `app/services/tts/segmentation.py`):
    script-run splitting, classifier disambiguation, low-confidence
    short-segment fallback, and adjacent same-language merging.
  - `concatenate_mp3_segments()` (`app/services/tts/audio_stitching.py`).
  - `POST /v1/tts/segment` (`app/routers/tts_segment.py`): success shape,
    400 on empty/oversized text, 429 passthrough, 500 on segmentation
    failure.

Router tests call the endpoint function directly with plain kwargs (a fake
`Request` + a plain `current_user` dict) rather than going through
TestClient/HTTP, mirroring the pattern established in
`tests/test_tts_public_access.py` and `tests/test_blackboards.py`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.tts import SegmentRequest, TextSegment
from app.services.tts.audio_stitching import concatenate_mp3_segments
from app.services.tts.segmentation import segment_text
import app.routers.tts_segment as tts_segment_module


USER_ID = "507f1f77bcf86cd799439011"


def _fake_request(payload) -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)
    return request


def _current_user() -> dict:
    return {"user_id": USER_ID, "email": "user@example.com"}


# ---------------------------------------------------------------------------
# segment_text() — script-run splitting (no classifier call for CJK/Cyrillic/
# Arabic/Devanagari/Hangul — deterministic by Unicode script alone)
# ---------------------------------------------------------------------------
class TestScriptSplitting:
    def test_cjk_latin_mixed_splits_by_script(self):
        segments = segment_text("Hello world. 你好世界。 This is English again.")
        lang_codes = [s.lang_code for s in segments]
        assert "en" in lang_codes
        assert "zh" in lang_codes
        # The Chinese segment must actually contain the Chinese characters.
        zh_segment = next(s for s in segments if s.lang_code == "zh")
        assert "你好世界" in zh_segment.text

    def test_cjk_side_never_calls_the_classifier(self):
        """Deterministic scripts (Han/Kana/Hangul/Cyrillic/Arabic/Devanagari)
        must resolve without ever building or calling the lingua classifier
        — that's the entire point of doing script-range splitting first."""
        from app.services.tts import segmentation as segmentation_module

        with patch.object(
            segmentation_module,
            "_get_latin_detector",
            side_effect=AssertionError("classifier must not be called for pure CJK text"),
        ):
            segments = segmentation_module.segment_text("你好世界。 日本語のテスト。")
        assert all(s.lang_code in {"zh", "ja"} for s in segments)

    def test_japanese_kanji_and_kana_stay_one_language(self):
        """Kanji (Han) + kana in the same sentence must resolve to 'ja', not
        fragment into alternating zh/ja micro-segments — see segmentation.py
        module docstring, step 1."""
        segments = segment_text("これはテストです。")
        assert len(segments) == 1
        assert segments[0].lang_code == "ja"

    def test_pure_chinese_text_resolves_to_zh(self):
        segments = segment_text("你好，世界。")
        assert len(segments) == 1
        assert segments[0].lang_code == "zh"

    def test_cyrillic_arabic_devanagari_hangul_are_deterministic(self):
        cases = {
            "ru": "Привет мир.",
            "ar": "مرحبا بالعالم.",
            "hi": "नमस्ते दुनिया।",
            "ko": "안녕하세요.",
        }
        for expected_lang, text in cases.items():
            segments = segment_text(text)
            assert len(segments) == 1
            assert segments[0].lang_code == expected_lang


# ---------------------------------------------------------------------------
# segment_text() — Latin-script classifier disambiguation
# ---------------------------------------------------------------------------
class TestLatinClassifierDisambiguation:
    def test_english_is_detected(self):
        segments = segment_text("Hello, how are you today my friend?")
        assert len(segments) == 1
        assert segments[0].lang_code == "en"

    def test_german_is_detected(self):
        segments = segment_text("Guten Morgen, wie geht es dir heute?")
        assert len(segments) == 1
        assert segments[0].lang_code == "de"

    def test_french_is_detected(self):
        segments = segment_text("Bonjour, comment allez-vous aujourd hui?")
        assert len(segments) == 1
        assert segments[0].lang_code == "fr"

    def test_mixed_en_de_fr_paragraph_is_disambiguated_per_sentence(self):
        text = (
            "Hello, how are you today my friend? "
            "Guten Morgen, wie geht es dir heute? "
            "Bonjour, comment allez-vous aujourd hui?"
        )
        segments = segment_text(text)
        lang_codes = [s.lang_code for s in segments]
        assert lang_codes == ["en", "de", "fr"]


# ---------------------------------------------------------------------------
# segment_text() — short low-confidence fallback
# ---------------------------------------------------------------------------
class TestLowConfidenceFallback:
    def test_short_ambiguous_segment_falls_back_to_default_lang(self):
        segments = segment_text("OK", default_lang="en")
        assert len(segments) == 1
        assert segments[0].lang_code == "en"

    def test_fallback_uses_caller_supplied_default_not_a_hardcoded_one(self):
        """The fallback must be the caller's default_lang, not always 'en' —
        otherwise this would be indistinguishable from a lucky guess."""
        segments = segment_text("OK", default_lang="de")
        assert len(segments) == 1
        assert segments[0].lang_code == "de"

    def test_long_latin_segment_is_not_forced_to_default_despite_dilution(self):
        """The <15-char guard must not apply to normal-length sentences —
        only short segments fall back on low confidence."""
        segments = segment_text(
            "Bonjour, comment allez-vous aujourd hui?", default_lang="en"
        )
        assert segments[0].lang_code == "fr"


# ---------------------------------------------------------------------------
# segment_text() — adjacent same-language merge
# ---------------------------------------------------------------------------
class TestAdjacentMerge:
    def test_adjacent_same_language_sentences_are_merged(self):
        segments = segment_text("Hello. How are you?")
        assert len(segments) == 1
        assert segments[0].text == "Hello. How are you?"
        assert segments[0].lang_code == "en"

    def test_merge_does_not_cross_a_language_boundary(self):
        segments = segment_text("Hello there. 你好。 Nice to meet you.")
        lang_codes = [s.lang_code for s in segments]
        assert lang_codes == ["en", "zh", "en"]
        # The two English sentences are NOT merged across the Chinese one.
        assert segments[0].text != segments[2].text


# ---------------------------------------------------------------------------
# segment_text() — empty / whitespace input
# ---------------------------------------------------------------------------
class TestEmptyInput:
    def test_empty_string_returns_empty_list(self):
        assert segment_text("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert segment_text("   \n\t  ") == []


# ---------------------------------------------------------------------------
# TextSegment / SegmentRequest models
# ---------------------------------------------------------------------------
class TestModels:
    def test_text_segment_round_trips(self):
        segment = TextSegment(text="Hello", lang_code="en")
        assert segment.text == "Hello"
        assert segment.lang_code == "en"

    def test_segment_request_rejects_over_max_length(self):
        with pytest.raises(ValidationError):
            SegmentRequest(text="x" * 10001)

    def test_segment_request_accepts_max_length_boundary(self):
        body = SegmentRequest(text="x" * 10000)
        assert len(body.text) == 10000


# ---------------------------------------------------------------------------
# concatenate_mp3_segments()
# ---------------------------------------------------------------------------
class TestAudioStitching:
    def test_concatenates_in_order(self):
        result = concatenate_mp3_segments([b"aaa", b"bbb", b"ccc"])
        assert result == b"aaabbbccc"

    def test_single_chunk_returned_as_is(self):
        assert concatenate_mp3_segments([b"solo"]) == b"solo"

    def test_empty_list_raises_value_error(self):
        with pytest.raises(ValueError):
            concatenate_mp3_segments([])


# ---------------------------------------------------------------------------
# POST /v1/tts/segment — router behavior
# ---------------------------------------------------------------------------
class TestSegmentEndpoint:
    @pytest.mark.asyncio
    async def test_success_returns_segments(self):
        rate_limit = AsyncMock(return_value=1)
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit):
            response = await tts_segment_module.segment_tts_text(
                request=_fake_request({"text": "Hello. Bonjour."}),
                current_user=_current_user(),
            )
        assert len(response.segments) >= 1
        assert all(isinstance(s, TextSegment) for s in response.segments)
        rate_limit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_text_is_400_and_skips_rate_limit(self):
        rate_limit = AsyncMock(return_value=1)
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit):
            with pytest.raises(HTTPException) as exc_info:
                await tts_segment_module.segment_tts_text(
                    request=_fake_request({"text": "   "}),
                    current_user=_current_user(),
                )
        assert exc_info.value.status_code == 400
        rate_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_text_field_is_400(self):
        rate_limit = AsyncMock(return_value=1)
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit):
            with pytest.raises(HTTPException) as exc_info:
                await tts_segment_module.segment_tts_text(
                    request=_fake_request({}),
                    current_user=_current_user(),
                )
        assert exc_info.value.status_code == 400
        rate_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_oversized_text_is_400_and_skips_rate_limit(self):
        """Field(max_length=10000) is enforced by SegmentRequest.model_validate
        inside the handler, and translated to a 400 (not FastAPI's default 422
        for a request-body schema violation) per the documented API contract."""
        rate_limit = AsyncMock(return_value=1)
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit):
            with pytest.raises(HTTPException) as exc_info:
                await tts_segment_module.segment_tts_text(
                    request=_fake_request({"text": "x" * 10001}),
                    current_user=_current_user(),
                )
        assert exc_info.value.status_code == 400
        rate_limit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rate_limit_429_propagates(self):
        rate_limit = AsyncMock(
            side_effect=HTTPException(
                status_code=429, detail="Too many segmentation requests. Please wait a moment."
            )
        )
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit):
            with pytest.raises(HTTPException) as exc_info:
                await tts_segment_module.segment_tts_text(
                    request=_fake_request({"text": "Hello world."}),
                    current_user=_current_user(),
                )
        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_segmentation_failure_is_500_with_generic_detail(self):
        """Classifier/regex internals must never leak into the client-facing
        error message."""
        rate_limit = AsyncMock(return_value=1)
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit), \
             patch.object(
                 tts_segment_module,
                 "segment_text",
                 side_effect=RuntimeError("lingua internal boom"),
             ):
            with pytest.raises(HTTPException) as exc_info:
                await tts_segment_module.segment_tts_text(
                    request=_fake_request({"text": "Hello world."}),
                    current_user=_current_user(),
                )
        assert exc_info.value.status_code == 500
        assert "lingua" not in exc_info.value.detail
        assert "boom" not in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_malformed_json_body_is_400(self):
        request = MagicMock()
        request.json = AsyncMock(side_effect=ValueError("not json"))
        rate_limit = AsyncMock(return_value=1)
        with patch.object(tts_segment_module, "enforce_user_rate_limit", rate_limit):
            with pytest.raises(HTTPException) as exc_info:
                await tts_segment_module.segment_tts_text(
                    request=request, current_user=_current_user()
                )
        assert exc_info.value.status_code == 400
        rate_limit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Route configuration — auth (router-level Depends) and rate-limit constants
# ---------------------------------------------------------------------------
class TestRouteConfiguration:
    def test_router_requires_firebase_auth(self):
        from app.auth.firebase_auth import get_firebase_user

        dependant_calls = [
            dep.dependency for dep in tts_segment_module.router.dependencies
        ]
        assert get_firebase_user in dependant_calls

    def test_router_is_not_tier_gated(self):
        """No get_subscription_tier dependency anywhere on this router —
        unlike /book/{book_id}/tts, this endpoint must be reachable by every
        authenticated tier."""
        import inspect

        source = inspect.getsource(tts_segment_module)
        assert "get_subscription_tier" not in source

    def test_rate_limit_is_configured_for_300_per_hour_separate_namespace(self):
        assert tts_segment_module._SEGMENT_RATE_LIMIT_MAX_REQUESTS == 300
        assert tts_segment_module._SEGMENT_RATE_LIMIT_WINDOW_SECONDS == 3600
        assert tts_segment_module._SEGMENT_RATE_LIMIT_FEATURE == "tts_segment"
        assert tts_segment_module._SEGMENT_RATE_LIMIT_FEATURE != "tts_amagic"

    def test_route_path_is_v1_tts_segment(self):
        paths = {route.path for route in tts_segment_module.router.routes}
        assert "/v1/tts/segment" in paths
