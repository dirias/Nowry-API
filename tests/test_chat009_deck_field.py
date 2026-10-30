"""CHAT-009 — a deck has a field: the learner's, else its voice language, else one saved guess."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from app.models.deck_settings import DeckFieldUpdate
from app.services import deck_field

DECK_ID = str(ObjectId())


def _completion(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_the_learners_choice_wins() -> None:
    deck = {"field": "math", "voice_settings": {"front": {"voice_lang": "ja-JP"}}, "field_guess": "language"}
    assert deck_field.resolve_known(deck) == ("math", "learner")


def test_a_voice_language_means_a_language_deck() -> None:
    assert deck_field.resolve_known({"voice_settings": {"back": {"voice_lang": "es-MX"}}}) == ("language", "language")


def test_a_saved_guess_is_used_before_guessing_again() -> None:
    assert deck_field.resolve_known({"field_guess": "science"}) == ("science", "guess")
    assert deck_field.resolve_known({}) is None


def test_the_answer_is_read_from_the_first_field_word() -> None:
    assert deck_field.parse_field("Certification.") == "certification"
    assert deck_field.parse_field("I think it's math") == "math"
    assert deck_field.parse_field("dunno") is None


@pytest.mark.asyncio
async def test_a_failed_guess_is_none_not_an_error() -> None:
    client = MagicMock()
    client.request.side_effect = TimeoutError()
    assert await deck_field.guess_field({}, [], client) is None
    assert await deck_field.guess_field({}, [], None) is None


def _cards_mock(cards: list[dict]):
    cursor = MagicMock()
    cursor.limit.return_value.to_list = AsyncMock(return_value=cards)
    collection = MagicMock()
    collection.find.return_value = cursor
    return collection


@pytest.mark.asyncio
async def test_the_route_guesses_once_and_saves_it() -> None:
    import app.routers.decks as decks

    llm = MagicMock()
    llm.request.return_value = _completion("science")
    collection = MagicMock()
    collection.update_one = AsyncMock()
    deck = {"_id": DECK_ID, "name": "Biology", "tags": ["cells"]}
    with patch.object(decks, "cards_collection", _cards_mock([{"title": "Mitochondria", "content": "powerhouse"}])), \
            patch.object(decks, "get_client_for_tier", return_value=llm):
        result = await decks.get_deck_field(DECK_ID, collection=collection, deck=deck)

    assert (result.field, result.source) == ("science", "guess")
    collection.update_one.assert_awaited_once_with({"_id": ObjectId(DECK_ID)}, {"$set": {"field_guess": "science"}})


@pytest.mark.asyncio
async def test_the_route_does_not_guess_when_the_deck_says() -> None:
    import app.routers.decks as decks

    with patch.object(decks, "get_client_for_tier") as client_for:
        result = await decks.get_deck_field(DECK_ID, collection=MagicMock(), deck={"_id": DECK_ID, "field": "math"})
    assert (result.field, result.source) == ("math", "learner")
    client_for.assert_not_called()


@pytest.mark.asyncio
async def test_setting_and_clearing_the_field() -> None:
    import app.routers.decks as decks

    collection = MagicMock()
    collection.update_one = AsyncMock()
    deck = {"_id": DECK_ID, "field": "math", "field_guess": "concept"}

    chosen = await decks.set_deck_field(DECK_ID, DeckFieldUpdate(field="certification"), collection=collection, deck=deck)
    assert (chosen.field, chosen.source) == ("certification", "learner")

    cleared = await decks.set_deck_field(DECK_ID, DeckFieldUpdate(field=None), collection=collection, deck=deck)
    assert (cleared.field, cleared.source) == ("concept", "guess")
