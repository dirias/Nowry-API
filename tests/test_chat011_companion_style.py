"""CHAT-011 — the learner's settings decide voice, length and method; the stage adds colour only (ADR-056)."""
import itertools

import pytest

from app.routers.agent import SPECIES_PERSONALITY_HINTS, ScreenContextPayload, _build_system_prompt
from app.services.companion_style import (
    CONCISENESS,
    LENGTH_RULES,
    STAGE_CHARACTER,
    TEACHING_RULES,
    TEACHING_STYLES,
    TONE_RULES,
    TONES,
    CompanionStyle,
    feedback_style_line,
    resolve_style,
)

USER = {"username": "rikydier", "preferences": {"general": {"primary_topic": "Japanese", "study_goal": "language"}}}
FRONT = ScreenContextPayload(page="study_session", deckId="d1", deckName="JPN N3", cardId="c1", front="持参する", isFlipped=False)
BACK = ScreenContextPayload(
    page="study_session", deckId="d1", cardId="c1", front="持参する", back="to bring", isFlipped=True
)
COMBOS = list(itertools.product(CONCISENESS, TONES, TEACHING_STYLES, STAGE_CHARACTER))

#: Lines that once overrode the learner's settings; none may come back.
OVERRIDES = (
    "Socratic",
    "follow-up question per reply",
    "End messages with a small question",
    "end with a short question",
    "Maximum 2 sentences",
    "first name",
    "Guide them",
    "Guide the user toward it",
)


def _prompt(style: CompanionStyle, stage: int = 1, ctx: ScreenContextPayload = FRONT, client: str = "phone") -> str:
    return _build_system_prompt(USER, True, style, screen_context=ctx, stage=stage, pet_name="Luna", client=client)


class TestResolve:
    def test_unset_is_balanced_friendly_show(self) -> None:
        assert resolve_style({}) == CompanionStyle("balanced", "friendly", "show")

    def test_a_socratic_tone_keeps_its_method_in_a_friendly_voice(self) -> None:
        assert resolve_style({"tone": "socratic"}) == CompanionStyle("balanced", "friendly", "guide")

    def test_a_chosen_style_outranks_the_old_socratic_tone(self) -> None:
        assert resolve_style({"tone": "socratic", "teaching_style": "show"}).teaching == "show"

    def test_unknown_values_fall_back(self) -> None:
        assert resolve_style({"conciseness": "huge", "tone": "pirate", "teaching_style": "x"}) == CompanionStyle()


@pytest.mark.parametrize("conciseness,tone,teaching,stage", COMBOS)
def test_only_the_chosen_lines_reach_the_prompt(conciseness: str, tone: str, teaching: str, stage: int) -> None:
    prompt = _prompt(CompanionStyle(conciseness, tone, teaching), stage)
    assert LENGTH_RULES[conciseness] in prompt and TONE_RULES[tone] in prompt and TEACHING_RULES[teaching] in prompt
    assert STAGE_CHARACTER[stage] in prompt
    assert prompt.count("RESPONSE LENGTH:") == 1 and prompt.count("TONE:") == 1 and prompt.count("TEACHING STYLE") == 1
    assert len(prompt.split()) < 1000
    for line in OVERRIDES:
        assert line not in prompt, line


def test_show_me_forbids_answering_with_a_question() -> None:
    prompt = _prompt(CompanionStyle(teaching="show"))
    assert "Never answer a request with a question" in prompt
    assert "guiding" not in prompt


def test_let_me_work_it_out_still_answers_a_tapped_question_first() -> None:
    assert "gets that content first" in _prompt(CompanionStyle(teaching="guide"))


def test_the_front_holds_back_only_the_meaning() -> None:
    front, back = _prompt(CompanionStyle()), _prompt(CompanionStyle(), ctx=BACK)
    assert "HIDDEN ANSWER" in front and "the app hides it" in front
    assert "HIDDEN ANSWER" not in back


def test_no_species_sets_a_method() -> None:
    for hint in SPECIES_PERSONALITY_HINTS.values():
        assert "ask questions" not in hint and "rather than just answers" not in hint


def test_feedback_carries_tone_and_length() -> None:
    line = feedback_style_line(CompanionStyle("concise", "strict", "show"))
    assert TONE_RULES["strict"] in line and LENGTH_RULES["concise"] in line
