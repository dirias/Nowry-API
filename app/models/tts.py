from __future__ import annotations

from pydantic import BaseModel, Field


class TTSRequest(BaseModel):
    text: str
    language_code: str = "en-US"
    auto_detect: bool = False


class TextSegment(BaseModel):
    """One language-homogeneous chunk of text produced by `segment_text()`."""

    text: str
    lang_code: str


class SegmentRequest(BaseModel):
    """Request body for `POST /v1/tts/segment`."""

    text: str = Field(..., max_length=10000)


class SegmentResponse(BaseModel):
    """Response body for `POST /v1/tts/segment`."""

    segments: list[TextSegment]
