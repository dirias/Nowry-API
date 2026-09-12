"""
Regression tests for the companion's screen context (PEND-003).

Reported as "when I ask the pet 'give me examples of this card', it sometimes
gives me an old card; if I say 'that's not the card' it gives the right one."

The cause was a spelling mismatch, not a prompt problem. The frontend builds
this payload in camelCase (`buildStudyContext` in `StudySession.js`) and
`ScreenContextPayload` declares it in snake_case. Pydantic ignores unknown
keys, so every study-session field arrived as None and the prompt named no
deck, no position and no side — leaving the chat history as the only thing
identifying which card, which is exactly why a correction fixed it.

The second half is a security rule rather than a quality one:
`strip_back_if_not_flipped` compares against `False`, and a field that never
arrives is None. The guard silently stopped firing, so the answer went to the
model while the learner was still looking at the question.

These tests are written against the WIRE SHAPE the web actually sends. A test
that constructed the model in snake_case would have passed throughout the bug.
"""
from app.routers.agent import ScreenContextPayload, _build_context_injection


def web_payload(**overrides):
    """Exactly what `buildStudyContext` puts on the wire."""
    payload = {
        "page": "study_session",
        "deckId": "deck-1",
        "deckName": "JPN",
        "cardIndex": 3,
        "totalCards": 20,
        "cardType": "basic",
        "isFlipped": False,
        "front": "ずいぶん",
        "back": None,
        "isDailyReview": False,
        "mode": "study",
    }
    payload.update(overrides)
    return payload


class TestTheWireShape:
    def test_every_camel_case_field_arrives(self):
        ctx = ScreenContextPayload(**web_payload())

        assert ctx.deck_id == "deck-1"
        assert ctx.deck_name == "JPN"
        assert ctx.card_index == 3
        assert ctx.total_cards == 20
        assert ctx.card_type == "basic"
        assert ctx.is_flipped is False
        assert ctx.is_daily_review is False

    def test_snake_case_is_still_accepted(self):
        """`populate_by_name` — no caller and no test is broken by the alias."""
        ctx = ScreenContextPayload(
            page="study_session", deck_name="JPN", card_index=3, total_cards=20, is_flipped=True
        )

        assert ctx.deck_name == "JPN"
        assert ctx.card_index == 3

    def test_the_book_page_travels_too(self):
        ctx = ScreenContextPayload(
            page="book", bookId="b1", bookTitle="Kafka", chapterTitle="One", visibleText="…"
        )

        assert ctx.book_id == "b1"
        assert ctx.book_title == "Kafka"
        assert ctx.chapter_title == "One"


class TestTheAnchorTheModelIsGiven:
    def test_the_card_is_named_by_position_and_deck(self):
        prompt = _build_context_injection(ScreenContextPayload(**web_payload()))

        assert "card 3 of 20" in prompt
        assert "JPN" in prompt

    def test_the_deck_id_reaches_the_tool_instruction(self):
        """Without it the companion cannot look the deck up at all."""
        prompt = _build_context_injection(ScreenContextPayload(**web_payload()))

        assert "deck-1" in prompt

    def test_a_daily_review_is_named_as_one(self):
        prompt = _build_context_injection(
            ScreenContextPayload(**web_payload(isDailyReview=True, deckName=None))
        )

        assert "Daily Review" in prompt

    def test_the_side_on_screen_is_stated(self):
        front = _build_context_injection(ScreenContextPayload(**web_payload()))
        back = _build_context_injection(
            ScreenContextPayload(**web_payload(isFlipped=True, back="Bastante / Mucho"))
        )

        assert "FRONT" in front
        assert "BACK" in back


class TestTheAnswerIsNotHandedOver:
    def test_the_back_is_stripped_while_the_question_is_showing(self):
        """
        The guard reads `is_flipped is False`. It only ever fires if that field
        survives the wire, which is the half of this bug that is a security
        rule rather than a quality one.
        """
        ctx = ScreenContextPayload(**web_payload(isFlipped=False, back="Bastante / Mucho"))

        assert ctx.back is None

    def test_the_back_survives_once_the_card_is_turned(self):
        ctx = ScreenContextPayload(**web_payload(isFlipped=True, back="Bastante / Mucho"))

        assert ctx.back == "Bastante / Mucho"

    def test_the_answer_is_not_in_the_prompt_while_the_question_is_showing(self):
        prompt = _build_context_injection(
            ScreenContextPayload(**web_payload(isFlipped=False, back="Bastante / Mucho"))
        )

        assert "Bastante / Mucho" not in prompt
