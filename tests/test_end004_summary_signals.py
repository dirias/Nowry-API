"""
END-004 (ADR-060 §4): the session's end feeds the companion the signals, not
the count; the prompt forbids the screen's numbers; the model may answer
NONE and the note is then nothing; the fallback speaks from the first line.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

if "app.auth.firebase_auth" not in sys.modules:
    _mock = MagicMock()
    _mock.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock

import pytest

USER_ID = "507f1f77bcf86cd799439011"


def _request(**over):
    from app.routers.agent import InterventionRequest

    base = dict(type="session_summary", session_total_cards=16, session_wrong_count=8)
    base.update(over)
    return InterventionRequest(**base)


async def _context(body):
    from app.routers.agent import _fetch_intervention_context

    cards = MagicMock()
    cards.find_one = AsyncMock(return_value=None)
    with patch("app.routers.agent.cards_collection", cards):
        return await _fetch_intervention_context(body.type, body, USER_ID)


def test_the_body_takes_signal_lines_and_the_score():
    body = _request(session_signal_lines=["験 family · 5 slipped · fast & wrong"], session_score=50, days_since_last_session=19)
    assert body.session_signal_lines == ["験 family · 5 slipped · fast & wrong"]
    assert body.session_score == 50


def test_more_than_eight_lines_are_refused():
    with pytest.raises(Exception):
        _request(session_signal_lines=[f"line {i}" for i in range(9)])


@pytest.mark.asyncio
async def test_the_context_carries_the_lines_and_not_the_count():
    ctx = await _context(_request(session_signal_lines=["験 family · 5 slipped · fast & wrong", "果物 · lapse"], session_score=50, days_since_last_session=19))
    assert ctx["signal_lines"] == "験 family · 5 slipped · fast & wrong\n果物 · lapse"
    assert ctx["score"] == 50
    assert ctx["days_away"] == 19
    assert "total_cards" not in ctx
    assert "duration_minutes" not in ctx


@pytest.mark.asyncio
async def test_an_old_client_without_lines_still_names_the_card_that_slipped_most():
    ctx = await _context(_request(most_missed_card_front="Photosynthesis"))
    assert ctx["signal_lines"] == "Photosynthesis"


def test_the_prompt_tells_the_model_what_the_screen_already_shows():
    from app.routers.agent import _INTERVENTION_PROMPT_TEMPLATES

    template = _INTERVENTION_PROMPT_TEMPLATES["session_summary"]
    assert "do not repeat any of those numbers" in template
    assert "reply with exactly: NONE" in template
    assert "{total_cards}" not in template and "{duration_minutes}" not in template


@pytest.mark.asyncio
async def test_none_from_the_model_is_no_note_and_no_fallback():
    from app.routers.agent import _generate_intervention_message

    fallback = MagicMock(return_value="a fallback sentence.")
    with patch("app.routers.agent.agent_llm") as agent:
        agent.chat = AsyncMock(return_value="NONE.")
        message = await _generate_intervention_message(
            "session_summary",
            {"score": 100, "days_away": "None", "signal_lines": "None", "most_attention_back": "None", "deck_name": "Kanji"},
            fallback,
            _request(),
        )
    assert message == ""
    fallback.assert_not_called()


def test_the_fallback_speaks_from_the_first_line_and_never_the_count():
    from app.routers.agent import _build_session_summary_message

    message = _build_session_summary_message(_request(session_signal_lines=["験 family · 5 slipped · fast & wrong"]))
    assert message.startswith("験 family · 5 slipped · fast & wrong")
    assert "16" not in message and "8 of" not in message
    assert "!" not in message


def test_the_fallback_is_silent_when_there_is_nothing_beyond_the_count():
    from app.routers.agent import _build_session_summary_message

    assert _build_session_summary_message(_request(session_wrong_count=0)) == ""
    assert _build_session_summary_message(_request()) == ""
