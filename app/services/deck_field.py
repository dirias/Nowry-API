"""Which field a deck is in, for the companion's ready questions (CHAT-009, ADR-055 §6).

The questions a chat offers depend on the subject: a word gets examples and a
memory hook, a maths problem gets "which rule applies?", an exam question gets
"why not B?". Decks carry no field, so this decides one, in order of trust:

1. the learner's own choice (``deck.field``);
2. a voice language saved on the deck, which means a language deck;
3. one guess from the deck's name, description, tags and ten cards, saved as
   ``deck.field_guess`` so it is made once per deck, never per message;
4. ``general``.
"""
import asyncio
import re
from typing import Literal, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

DeckField = Literal["language", "math", "science", "certification", "concept", "general"]
FIELDS: tuple[str, ...] = ("language", "math", "science", "certification", "concept", "general")
FieldSource = Literal["learner", "language", "guess", "default"]

#: Cards read for the guess; enough to tell vocabulary from formulas, bounded.
GUESS_SAMPLE_CARDS = 10
_GUESS_CHARS_PER_SIDE = 120


def saved_voice_language(deck: dict) -> Optional[str]:
    """The language saved for either side's voice, if any."""
    voices = deck.get("voice_settings") or {}
    for side in ("front", "back"):
        lang = (voices.get(side) or {}).get("voice_lang")
        if lang:
            return str(lang)
    return None


def resolve_known(deck: dict) -> Optional[tuple[str, FieldSource]]:
    """The field when the deck already says it, or None when a guess is needed."""
    if deck.get("field") in FIELDS:
        return deck["field"], "learner"
    if saved_voice_language(deck):
        return "language", "language"
    if deck.get("field_guess") in FIELDS:
        return deck["field_guess"], "guess"
    return None


def guess_prompt(deck: dict, cards: list[dict]) -> str:
    """The one-word classification prompt for a deck."""
    def clip(text: object) -> str:
        return str(text or "").replace("\n", " ")[:_GUESS_CHARS_PER_SIDE]

    samples = "\n".join(f"- {clip(card.get('title'))} → {clip(card.get('content'))}" for card in cards)
    tags = ", ".join(str(tag) for tag in (deck.get("tags") or [])[:10])
    return (
        "Classify this flashcard deck into exactly one field:\n"
        "language (vocabulary or grammar of a human language), math (problems, formulas, proofs), "
        "science (physics, chemistry, biology, medicine), certification (exam-style questions for a "
        "professional test), concept (law, history, theory, definitions), general (anything else).\n\n"
        f"Deck name: {clip(deck.get('name'))}\n"
        f"Description: {clip(deck.get('description'))}\n"
        f"Tags: {tags}\n"
        f"Sample cards:\n{samples}\n\n"
        "Answer with the one field word only."
    )


def parse_field(raw: str) -> Optional[str]:
    """The first field word in a model's answer, or None."""
    for word in re.findall(r"[a-z]+", (raw or "").lower()):
        if word in FIELDS:
            return word
    return None


async def guess_field(deck: dict, cards: list[dict], llm_client) -> Optional[str]:
    """One model guess, or None when the model is unavailable or says nothing usable."""
    if llm_client is None:
        return None
    try:
        response = await asyncio.to_thread(llm_client.request, guess_prompt(deck, cards))
        return parse_field(response.choices[0].message.content)
    except Exception as exc:
        # A guess is a nicety: the chat still works with "general".
        logger.warning("[deck_field] guess failed for deck %s: %s", deck.get("_id"), exc)
        return None
