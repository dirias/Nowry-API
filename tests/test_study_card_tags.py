"""
Tags validation on the StudyCard Pydantic model (POST /study-cards).

The frontend previously only ever sent fixed literal tags ('generated',
'quiz') on card creation. Now users can type/select arbitrary free-text
tags per batch, so `StudyCard.tags` needs real server-side validation for
the first time: trimming, dropping blanks, a per-tag length cap, a
per-card count cap, and case-insensitive dedup.

These are unit tests against the model directly (`StudyCard(**payload)`),
mirroring how FastAPI validates the request body for
`POST /study-cards` since `create_study_card` takes `card: StudyCard` as
its body parameter — no HTTP/DB layer needs to be exercised to cover the
validator itself.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.StudyCard import StudyCard


def _base_payload(**overrides: object) -> dict:
    payload = {
        "title": "Test Card",
        "content": "Some content",
    }
    payload.update(overrides)
    return payload


def test_tags_are_trimmed() -> None:
    card = StudyCard(**_base_payload(tags=["  physics  ", "quantum\t", " science"]))
    assert card.tags == ["physics", "quantum", "science"]


def test_blank_tags_are_dropped_without_error() -> None:
    card = StudyCard(**_base_payload(tags=["physics", "   ", "", "quantum"]))
    assert card.tags == ["physics", "quantum"]


def test_none_tags_stays_none() -> None:
    card = StudyCard(**_base_payload(tags=None))
    assert card.tags is None


def test_tag_over_max_length_is_rejected() -> None:
    too_long_tag = "a" * 41
    with pytest.raises(ValidationError) as exc_info:
        StudyCard(**_base_payload(tags=["physics", too_long_tag]))
    assert "exceeds maximum length" in str(exc_info.value)


def test_tag_at_max_length_is_accepted() -> None:
    exactly_max_tag = "a" * 40
    card = StudyCard(**_base_payload(tags=[exactly_max_tag]))
    assert card.tags == [exactly_max_tag]


def test_more_than_ten_tags_is_rejected() -> None:
    eleven_unique_tags = [f"tag{i}" for i in range(11)]
    with pytest.raises(ValidationError) as exc_info:
        StudyCard(**_base_payload(tags=eleven_unique_tags))
    assert "cannot have more than 10 tags" in str(exc_info.value)


def test_exactly_ten_tags_is_accepted() -> None:
    ten_unique_tags = [f"tag{i}" for i in range(10)]
    card = StudyCard(**_base_payload(tags=ten_unique_tags))
    assert card.tags == ten_unique_tags


def test_tags_are_deduped_case_insensitively_keeping_first_seen_casing() -> None:
    card = StudyCard(**_base_payload(tags=["Physics", "physics", "PHYSICS", "Quantum"]))
    assert card.tags == ["Physics", "Quantum"]


def test_dedup_happens_before_max_count_check() -> None:
    """Duplicates (case-insensitive) collapse first, so 12 submitted tags that
    dedupe down to 10 unique tags should be accepted, not rejected."""
    tags = [f"tag{i}" for i in range(10)] + ["Tag0", "TAG1"]
    card = StudyCard(**_base_payload(tags=tags))
    assert card.tags == [f"tag{i}" for i in range(10)]
