"""CHAT-016 — quiz feedback follows the learner's tone and reply length (ADR-056 §6)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from app.routers.quiz import _learner_style_line, _style_section
from app.services.companion_style import LENGTH_RULES, TONE_RULES


@pytest.mark.asyncio
async def test_the_learners_tone_and_length_are_read() -> None:
    users = MagicMock()
    users.find_one = AsyncMock(return_value={"preferences": {"agent": {"tone": "strict", "conciseness": "concise"}}})
    with patch("app.routers.quiz.users_collection", users):
        line = await _learner_style_line(str(ObjectId()))
    assert TONE_RULES["strict"] in line and LENGTH_RULES["concise"] in line


@pytest.mark.asyncio
async def test_an_unknown_learner_gets_the_defaults() -> None:
    users = MagicMock()
    users.find_one = AsyncMock(return_value=None)
    with patch("app.routers.quiz.users_collection", users):
        line = await _learner_style_line(str(ObjectId()))
    assert TONE_RULES["friendly"] in line and LENGTH_RULES["balanced"] in line


def test_the_section_is_empty_without_a_style() -> None:
    assert _style_section("") == ""
    assert _style_section("TONE: x").startswith("LEARNER'S STYLE")
