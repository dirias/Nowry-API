# app/models/CardGenerationRequest.py
"""Request model and adaptive-cap policy for the AI card generation endpoints.

sampleNumber semantics (POST /card/generate and /card/generate/stream):
    None => adaptive mode — the server derives an effective card cap from
            sampleText density via compute_effective_cap().
    int  => fixed mode — legacy exact-count behavior, unchanged.
"""
from math import ceil
from typing import Annotated, Optional

from pydantic import BaseModel, Field, StringConstraints

# Adaptive-mode guardrails.
ADAPTIVE_MIN_CARDS: int = 3
ADAPTIVE_MAX_CARDS: int = 50
ADAPTIVE_CHARS_PER_CARD: int = 250
# A line counts toward the cap only if it has at least this many
# non-whitespace characters.
MIN_LINE_CONTENT_CHARS: int = 3

ExcludedTitle = Annotated[str, StringConstraints(max_length=200)]


class CardGenerationRequest(BaseModel):
    """Request body shared by POST /card/generate and /card/generate/stream."""

    prompt: str = Field(max_length=5000)
    sampleText: str = Field(min_length=1, max_length=20000)
    sampleNumber: Optional[int] = Field(default=None, ge=1, le=50)
    excludeTitles: list[ExcludedTitle] = Field(default_factory=list, max_length=50)


def compute_effective_cap(sample_text: str, sample_number: Optional[int]) -> int:
    """Return the effective card cap for one generation request.

    Fixed mode (sample_number is an int): the cap is exactly sample_number.
    Adaptive mode (sample_number is None): deterministic content-derived cap,
        max(non_empty_lines, ceil(len(sample_text) / 250))
    clamped to [ADAPTIVE_MIN_CARDS, ADAPTIVE_MAX_CARDS], where a non-empty
    line has >= MIN_LINE_CONTENT_CHARS non-whitespace characters.
    """
    if sample_number is not None:
        return sample_number
    non_empty_lines: int = sum(
        1
        for line in sample_text.splitlines()
        if sum(1 for char in line if not char.isspace()) >= MIN_LINE_CONTENT_CHARS
    )
    char_cap: int = ceil(len(sample_text) / ADAPTIVE_CHARS_PER_CARD)
    raw_cap: int = max(non_empty_lines, char_cap)
    return max(ADAPTIVE_MIN_CARDS, min(ADAPTIVE_MAX_CARDS, raw_cap))
