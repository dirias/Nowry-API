"""
Tests for avatar prompt personalisation.

The generated pet is supposed to reflect the person it belongs to — their
accent colour, their topics, their study goal, their species, and the stage
their companion has reached. It largely did not:

  * Colour was read from `preferences.pet.pet_color`, a slug with no picker
    anywhere in the UI. It was null for effectively every user, so the
    `or "violet"` fallback fired and every generated pet came out violet
    regardless of the accent the user had actually chosen.
  * Topic lookups were substring tests, so "art" matched
    "artificial_intelligence" and every AI learner was handed a paintbrush.
  * The scene table was keyed on "programming"/"coding" while the app stores
    "technology", so the most common topic matched nothing. Five of fourteen
    topics had a scene at all.
  * The stage's *name* never reached the prompt, so the six forms differed
    only by a size adjective.

These lock the coverage and the wiring, because none of the above is a crash —
it is a prompt that is quietly wrong, which no type checker would catch.
"""
import sys
from unittest.mock import MagicMock

import pytest

for _mod in (
    "google.generativeai",
    "google.generativeai.types",
    "google.generativeai.protos",
    "google.api_core.exceptions",
):
    sys.modules.setdefault(_mod, MagicMock())

# Another suite in this run may already have registered app.models.quiz as a
# bare MagicMock; its .QuizConfig is then itself a MagicMock, which breaks
# Pydantic's `QuizConfig | None` schema generation in agent.py's ChatResponse
# at import time. Unconditionally supply real models — same fix and same
# reasoning as test_tracing.py's _ensure_agent_importable().
from typing import Optional as _Optional  # noqa: E402
from pydantic import BaseModel as _BM  # noqa: E402


class _QuizConfig(_BM):
    mode: str
    topic: _Optional[str] = None
    question_count: int = 10
    deck_id: _Optional[str] = None


class _QuizOffer(_BM):
    topic: str
    mode: str = "ai"
    question_count: int = 10


_quiz_mod = sys.modules.setdefault("app.models.quiz", MagicMock())
_quiz_mod.QuizConfig = _QuizConfig
_quiz_mod.QuizOffer = _QuizOffer

from app.routers.agent import (  # noqa: E402
    AVATAR_GOAL_MOODS,
    AVATAR_INTEREST_TRAITS,
    AVATAR_STAGE_NAMES,
    AVATAR_THEME_COLOR_NAMES,
    AVATAR_TOPIC_SCENES,
    _build_avatar_prompt,
    _canonical_topic,
    _describe_theme_color,
)

# The canonical lists from nowry/src/constants/learningTaxonomy.js.
TOPICS = [
    "artificial_intelligence", "technology", "science", "mathematics",
    "history", "languages", "literature", "art", "music", "business",
    "health", "philosophy", "design", "psychology",
]
STUDY_GOALS = ["general", "academic", "career", "language", "hobby"]

# getColorPresets() in nowry/src/theme/colorSchemeGenerator.js.
ACCENT_PRESETS = [
    "#2a6971", "#0b6bcb", "#9c27b0", "#e91e63",
    "#f44336", "#ff9800", "#4caf50", "#795548",
]


def build_user(theme_color="#2a6971", interests=None, primary_topic="technology",
               study_goal="general", species="owl", username="Didier Irias"):
    return {
        "username": username,
        "preferences": {
            "pet": {"pet_species": species, "avatar_seed": "11111111-1111-1111-1111-111111111111"},
            "general": {
                "interests": interests if interests is not None else ["history", "technology"],
                "primary_topic": primary_topic,
                "study_goal": study_goal,
                "theme_color": theme_color,
            },
        },
    }


class TestTaxonomyCoverage:
    """Every value the app can store must produce personalisation."""

    @pytest.mark.parametrize("topic", TOPICS)
    def test_every_topic_has_interest_traits(self, topic: str) -> None:
        assert AVATAR_INTEREST_TRAITS.get(topic)

    @pytest.mark.parametrize("topic", TOPICS)
    def test_every_topic_has_a_scene(self, topic: str) -> None:
        assert AVATAR_TOPIC_SCENES.get(topic)

    @pytest.mark.parametrize("goal", STUDY_GOALS)
    def test_every_study_goal_has_a_mood(self, goal: str) -> None:
        assert AVATAR_GOAL_MOODS.get(goal)

    def test_every_stage_has_a_name(self) -> None:
        assert sorted(AVATAR_STAGE_NAMES) == [1, 2, 3, 4, 5, 6]

    def test_tables_carry_no_entries_outside_the_taxonomy(self) -> None:
        # Dead keys ("nature", "cooking") meant the tables looked better
        # covered than they were.
        assert set(AVATAR_INTEREST_TRAITS) <= set(TOPICS)
        assert set(AVATAR_TOPIC_SCENES) <= set(TOPICS)

    def test_each_topic_gets_its_own_distinct_traits(self) -> None:
        firsts = [traits[0] for traits in AVATAR_INTEREST_TRAITS.values()]
        assert len(set(firsts)) == len(firsts)

    def test_each_topic_gets_its_own_distinct_scene(self) -> None:
        scenes = list(AVATAR_TOPIC_SCENES.values())
        assert len(set(scenes)) == len(scenes)


class TestTopicMatching:
    def test_ai_does_not_borrow_arts_traits(self) -> None:
        # The original substring lookup matched "art" inside
        # "artificial_intelligence".
        ai = AVATAR_INTEREST_TRAITS["artificial_intelligence"]
        assert ai != AVATAR_INTEREST_TRAITS["art"]
        prompt, _ = _build_avatar_prompt(
            build_user(interests=["artificial_intelligence"], primary_topic="artificial_intelligence"), 3
        )
        assert "paintbrush" not in prompt
        assert "neural-network" in prompt

    def test_literature_is_recognised(self) -> None:
        # It matched nothing at all before.
        prompt, _ = _build_avatar_prompt(
            build_user(interests=["literature"], primary_topic="literature"), 3
        )
        assert "hardback book" in prompt
        assert "reading nook" in prompt

    @pytest.mark.parametrize(
        "legacy,expected",
        [("japanese", "languages"), ("coding", "technology"), ("programming", "technology"),
         ("math", "mathematics"), ("biology", "science"), ("LANGUAGE", "languages")],
    )
    def test_legacy_topic_values_still_resolve(self, legacy: str, expected: str) -> None:
        assert _canonical_topic(legacy) == expected

    def test_unknown_topic_degrades_to_no_scene_rather_than_a_wrong_one(self) -> None:
        prompt, _ = _build_avatar_prompt(build_user(primary_topic="underwater_basket_weaving"), 3)
        assert prompt  # still builds
        assert not any(scene.strip(", ") in prompt for scene in AVATAR_TOPIC_SCENES.values())


class TestColour:
    @pytest.mark.parametrize("accent", ACCENT_PRESETS)
    def test_every_accent_preset_has_its_own_wording(self, accent: str) -> None:
        assert AVATAR_THEME_COLOR_NAMES[accent]

    def test_preset_names_are_all_distinct(self) -> None:
        names = list(AVATAR_THEME_COLOR_NAMES.values())
        assert len(set(names)) == len(names)

    def test_the_users_accent_reaches_the_prompt(self) -> None:
        # The reported bug: a green accent produced a violet pet.
        prompt, _ = _build_avatar_prompt(build_user(theme_color="#4caf50"), 2)
        assert "fresh forest green" in prompt
        assert "violet" not in prompt

    def test_a_custom_hex_is_described_by_hue_not_defaulted(self) -> None:
        assert _describe_theme_color("#1e8f3a") == "fresh green"
        assert _describe_theme_color("#c2185b") == "vivid magenta"

    def test_a_hex_without_its_hash_still_resolves(self) -> None:
        assert _describe_theme_color("4caf50") == _describe_theme_color("#4caf50")

    def test_grey_is_named_grey_rather_than_a_random_hue(self) -> None:
        assert "grey" in _describe_theme_color("#777777")

    @pytest.mark.parametrize("bad", [None, "", "not-a-color", "#12", "#zzzzzz"])
    def test_unusable_input_falls_back_without_throwing(self, bad) -> None:
        assert _describe_theme_color(bad) == AVATAR_THEME_COLOR_NAMES["#2a6971"]


class TestPromptComposition:
    def test_every_configured_dimension_reaches_the_prompt(self) -> None:
        prompt, _ = _build_avatar_prompt(
            build_user(theme_color="#e91e63", interests=["music"], primary_topic="music",
                       study_goal="hobby", species="dragon"),
            5,
        )
        assert "Oracle" in prompt              # stage name
        assert "elder sage" in prompt          # stage descriptor
        assert "dragon" in prompt              # species
        assert "musical notes" in prompt       # interest trait
        assert "vivid rose pink" in prompt     # accent colour
        assert "playful atmosphere" in prompt  # study goal
        assert "spotlight" in prompt           # topic scene

    def test_the_stage_name_changes_across_the_arc(self) -> None:
        names = []
        for stage in range(1, 7):
            prompt, _ = _build_avatar_prompt(build_user(), stage)
            names.append(prompt.split("—")[0].strip())
        assert names == [f"a {AVATAR_STAGE_NAMES[s]}" for s in range(1, 7)]

    def test_two_differently_configured_users_get_different_prompts(self) -> None:
        a, _ = _build_avatar_prompt(
            build_user(theme_color="#4caf50", interests=["artificial_intelligence"],
                       primary_topic="artificial_intelligence", study_goal="career", species="robot"), 4)
        b, _ = _build_avatar_prompt(
            build_user(theme_color="#e91e63", interests=["literature"],
                       primary_topic="literature", study_goal="hobby", species="cat"), 4)
        assert a != b

    def test_a_user_with_nothing_configured_still_gets_a_valid_prompt(self) -> None:
        prompt, seed = _build_avatar_prompt(
            {"username": "", "preferences": {"pet": {}, "general": {}}}, 1
        )
        assert "Wisp" in prompt
        assert "owl companion" in prompt
        assert isinstance(seed, int)

    def test_the_seed_is_stable_for_the_same_user(self) -> None:
        # Same seed => the same creature across regenerations.
        _, first = _build_avatar_prompt(build_user(), 1)
        _, second = _build_avatar_prompt(build_user(), 4)
        assert first == second
