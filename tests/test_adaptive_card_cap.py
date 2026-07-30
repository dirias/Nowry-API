"""Adaptive card-count contract tests — CardGenerationRequest + cap policy.

Covers:
  1. compute_effective_cap: fixed-mode passthrough, adaptive floor/ceiling,
     line-derived vs char-derived caps, and the >=3 non-whitespace-chars
     line-counting rule
  2. CardGenerationRequest validation: sampleNumber optional (None = adaptive),
     ge=1/le=50 bounds, excludeTitles bounded at 50 entries x 200 chars
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.CardGenerationRequest import (
    ADAPTIVE_MAX_CARDS,
    ADAPTIVE_MIN_CARDS,
    CardGenerationRequest,
    compute_effective_cap,
)


# ---------------------------------------------------------------------------
# compute_effective_cap
# ---------------------------------------------------------------------------

def test_fixed_mode_returns_sample_number_verbatim():
    """Fixed mode: the cap is exactly the requested count, content ignored."""
    assert compute_effective_cap("x" * 20000, 2) == 2
    assert compute_effective_cap("hi", 50) == 50


def test_adaptive_floor_is_three_for_tiny_text():
    """One short line, few chars -> clamped up to the floor of 3."""
    assert compute_effective_cap("word", None) == ADAPTIVE_MIN_CARDS


def test_adaptive_ceiling_is_fifty():
    """20k chars => char_cap 80; 100 lines — both clamp down to 50."""
    text = "\n".join(["line %03d of the source" % i for i in range(100)])
    text += "x" * (20000 - len(text))
    assert compute_effective_cap(text[:20000], None) == ADAPTIVE_MAX_CARDS


def test_adaptive_line_cap_wins_for_term_lists():
    """10 short definition lines (~230 chars total): lines beat char cap."""
    text = "\n".join(f"term{i} — definition {i}" for i in range(10))
    assert len(text) < 250 * 9  # char_cap < 10, so lines dominate
    assert compute_effective_cap(text, None) == 10


def test_adaptive_char_cap_wins_for_dense_paragraph():
    """A single 1000-char paragraph: 1 line, ceil(1000/250) = 4 cards."""
    text = "a" * 1000
    assert compute_effective_cap(text, None) == 4


def test_adaptive_ignores_lines_under_three_content_chars():
    """Lines with < 3 non-whitespace chars don't count toward the line cap."""
    meaningful = [f"line {i} content here" for i in range(6)]
    noise = ["", "  ", "a", " ab ", "--"]
    text = "\n".join(noise + meaningful + noise)
    assert compute_effective_cap(text, None) == 6


def test_adaptive_is_deterministic():
    """Same input -> same cap, every time."""
    text = "alpha beta\ngamma delta\n" * 40
    caps = {compute_effective_cap(text, None) for _ in range(5)}
    assert len(caps) == 1


# ---------------------------------------------------------------------------
# CardGenerationRequest validation
# ---------------------------------------------------------------------------

def test_request_sample_number_defaults_to_none_adaptive():
    """Omitting sampleNumber selects adaptive mode (None)."""
    req = CardGenerationRequest(prompt="p", sampleText="some text")
    assert req.sampleNumber is None
    assert req.excludeTitles == []


def test_request_fixed_mode_backward_compatible():
    """Legacy callers sending sampleNumber keep exact-count behavior."""
    req = CardGenerationRequest(prompt="p", sampleText="t", sampleNumber=2)
    assert req.sampleNumber == 2


@pytest.mark.parametrize("bad_number", [0, 51, -1])
def test_request_sample_number_bounds(bad_number: int):
    """sampleNumber outside [1, 50] is rejected."""
    with pytest.raises(ValidationError):
        CardGenerationRequest(prompt="p", sampleText="t", sampleNumber=bad_number)


def test_request_exclude_titles_accepted_within_bounds():
    titles = [f"Concept {i}" for i in range(50)]
    req = CardGenerationRequest(prompt="p", sampleText="t", excludeTitles=titles)
    assert len(req.excludeTitles) == 50


def test_request_exclude_titles_rejects_more_than_fifty():
    titles = [f"Concept {i}" for i in range(51)]
    with pytest.raises(ValidationError):
        CardGenerationRequest(prompt="p", sampleText="t", excludeTitles=titles)


def test_request_exclude_titles_rejects_title_over_200_chars():
    with pytest.raises(ValidationError):
        CardGenerationRequest(
            prompt="p", sampleText="t", excludeTitles=["x" * 201]
        )
