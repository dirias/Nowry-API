"""
Two faults in the companion's proactive messages, both found by reading one.

A summary on a phone said: "You reviewed 2 cards and no cards need additional
practice. The deck this deck had no cards identified as needing most attention."

1. `_fetch_intervention_context` sets `ctx["deck_name"] = "this deck"` for both
   `wrong_answer` and `session_summary`, and the prompt hands that to the model
   as `Deck name: {deck_name}`. A model told the deck is called "this deck"
   quite reasonably writes "The deck this deck". The placeholder was a PHRASE
   standing where a NAME was promised, and the system prompt's own rule 6 —
   never invent a deck name — is exactly why the model would not paper over it.

2. `/agent/chat` takes a `language` and mirrors it. `/agent/intervention` takes
   none and its system prompt says "Write in plain English", so a Spanish
   learner gets a Spanish answer when they ask and an English one when the
   companion speaks first — about the same card, seconds apart.

Both affect the web identically; the phone is only where they were seen.
"""
import sys

from tests._stubs import stub_if_missing
from unittest.mock import AsyncMock, MagicMock, patch

stub_if_missing("bcrypt")

if "app.auth.firebase_auth" not in sys.modules:
    _mock = MagicMock()
    _mock.get_firebase_user = MagicMock()
    sys.modules["app.auth.firebase_auth"] = _mock

import pytest

USER_ID = "507f1f77bcf86cd799439011"


def _request(**over):
    from app.routers.agent import InterventionRequest

    base = dict(
        type="session_summary",
        session_total_cards=2,
        session_wrong_count=0,
    )
    base.update(over)
    return InterventionRequest(**base)


async def _context(body, *, card=None, deck=None):
    from app.routers.agent import _fetch_intervention_context

    cards = MagicMock()
    cards.find_one = AsyncMock(return_value=card)
    cards.count_documents = AsyncMock(return_value=0)
    decks = MagicMock()
    decks.find_one = AsyncMock(return_value=deck)

    with patch("app.routers.agent.cards_collection", cards), \
         patch("app.routers.agent.decks_collection", decks):
        return await _fetch_intervention_context(body.type, body, USER_ID)


# ── The deck's name ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_summary_never_calls_the_deck_this_deck():
    """The placeholder read as a name and the model used it as one."""
    ctx = await _context(_request())
    assert ctx["deck_name"] != "this deck"


@pytest.mark.asyncio
async def test_a_wrong_answer_never_calls_the_deck_this_deck():
    ctx = await _context(
        _request(type="wrong_answer", card_id="507f1f77bcf86cd799439012", card_front="ずいぶん", card_back="considerably")
    )
    assert ctx["deck_name"] != "this deck"


@pytest.mark.asyncio
async def test_the_real_deck_name_is_used_when_the_caller_sends_one():
    ctx = await _context(_request(deck_name="JLPT N3"))
    assert ctx["deck_name"] == "JLPT N3"


@pytest.mark.asyncio
async def test_an_unknown_deck_is_named_the_way_every_other_absent_field_is():
    """`card_back` is "Unknown" and `card_notes` is "None provided" when absent.
    An unknown deck says the same kind of thing, so rule 6 can do its job."""
    ctx = await _context(_request())
    assert ctx["deck_name"] == "Unknown"


# ── The language ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_model_is_told_which_language_to_answer_in():
    from app.routers.agent import _generate_intervention_message

    llm = AsyncMock(return_value="una frase")
    with patch("app.routers.agent.agent_llm") as agent:
        agent.chat = llm
        await _generate_intervention_message(
            "session_summary",
            {
                "total_cards": 2,
                "needs_practice_count": 0,
                "most_attention_front": "None",
                "most_attention_back": "None",
                "duration_minutes": 0,
                "deck_name": "Unknown",
            },
            lambda body: "fallback",
            _request(language="es"),
        )

    system = llm.await_args.kwargs["system_prompt"]
    assert "Spanish" in system, "the learner's language must reach the model"


@pytest.mark.asyncio
async def test_english_is_the_default_rather_than_the_only_option():
    from app.routers.agent import _generate_intervention_message

    llm = AsyncMock(return_value="a sentence")
    with patch("app.routers.agent.agent_llm") as agent:
        agent.chat = llm
        await _generate_intervention_message(
            "streak_milestone",
            {"streak_count": 7, "science_note": "n/a"},
            lambda body: "fallback",
            _request(type="streak_milestone", streak_count=7),
        )

    assert "English" in llm.await_args.kwargs["system_prompt"]


def test_the_request_carries_a_language_like_chat_does():
    """`ChatRequest.language` has defaulted to 'en' since the endpoint shipped."""
    assert _request().language == "en"
    assert _request(language="ja").language == "ja"
