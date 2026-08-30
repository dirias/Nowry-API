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
    _strip_generated_backdrop,
    AVATAR_TOPIC_SIGNATURES,
    DEFAULT_PET_NAME,
    MAX_AVATAR_TOPICS,
    _is_default_companion,
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


class TestRankedTopics:
    """
    The picker takes five ranked topics; the prompt used to read only two, and
    hung both on the creature as props. Rank now carries meaning: #1 shapes the
    plumage, the rest add accents.
    """

    @pytest.mark.parametrize("topic", TOPICS)
    def test_every_topic_can_be_a_primary_signature(self, topic: str) -> None:
        assert AVATAR_TOPIC_SIGNATURES.get(topic)

    def test_signatures_are_distinct_and_inside_the_taxonomy(self) -> None:
        assert set(AVATAR_TOPIC_SIGNATURES) <= set(TOPICS)
        sigs = list(AVATAR_TOPIC_SIGNATURES.values())
        assert len(set(sigs)) == len(sigs)

    def test_all_five_ranked_topics_reach_the_prompt(self) -> None:
        five = ["artificial_intelligence", "science", "music", "technology", "health"]
        prompt, _ = _build_avatar_prompt(
            build_user(interests=five, primary_topic="artificial_intelligence"), 4)
        assert "neural-network nodes" in prompt   # #1, as a plumage signature
        assert "lab coat" in prompt               # #2 science
        assert "musical notes" in prompt          # #3 music
        assert "circuit board" in prompt          # #4 technology
        assert "medical cross" in prompt          # #5 health

    def test_the_primary_shapes_the_creature_not_just_its_props(self) -> None:
        five = ["artificial_intelligence", "science", "music", "technology", "health"]
        ai_first, _ = _build_avatar_prompt(build_user(interests=five, primary_topic="artificial_intelligence"), 4)
        music_first, _ = _build_avatar_prompt(
            build_user(interests=["music"] + [t for t in five if t != "music"], primary_topic="music"), 4)
        # Same five topics, different rank 1 => a different creature.
        assert ai_first != music_first
        assert AVATAR_TOPIC_SIGNATURES["artificial_intelligence"] in ai_first
        assert AVATAR_TOPIC_SIGNATURES["music"] in music_first

    def test_the_primary_is_not_also_repeated_as_an_accent(self) -> None:
        prompt, _ = _build_avatar_prompt(
            build_user(interests=["music", "science"], primary_topic="music"), 4)
        assert prompt.count(AVATAR_INTEREST_TRAITS["music"][0]) == 0

    def test_a_single_topic_user_still_gets_an_accessory(self) -> None:
        prompt, _ = _build_avatar_prompt(build_user(interests=["music"], primary_topic="music"), 4)
        assert AVATAR_TOPIC_SIGNATURES["music"] in prompt
        assert AVATAR_INTEREST_TRAITS["music"][0] in prompt

    def test_topics_beyond_the_cap_are_ignored_rather_than_bloating_the_prompt(self) -> None:
        assert MAX_AVATAR_TOPICS == 5
        many = TOPICS[:8]
        prompt, _ = _build_avatar_prompt(build_user(interests=many, primary_topic=many[0]), 4)
        used = sum(1 for t in many if AVATAR_INTEREST_TRAITS[t][0] in prompt)
        assert used <= MAX_AVATAR_TOPICS


class TestDefaultCompanion:
    """Nowry stands in until the user has a portrait of their own."""

    def test_a_user_with_no_portrait_is_on_the_default_companion(self) -> None:
        assert _is_default_companion({}) is True
        assert _is_default_companion({"pet_species": "owl"}) is True

    def test_generating_a_portrait_ends_the_default_state(self) -> None:
        assert _is_default_companion({"avatar_url": "https://example.com/a.png"}) is False

    def test_the_default_is_named_after_the_brand(self) -> None:
        assert DEFAULT_PET_NAME == "Nowry"


class TestGeneratedBackdrop:
    """
    FLUX cannot emit alpha, so every portrait arrives on a solid white field.
    Left in place it shows as a white disc behind the pet in the orb, and it
    turns a locked look-ahead form's silhouette into a featureless square
    instead of the creature's shape.
    """

    @staticmethod
    def _flat_character():
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (300, 300), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.ellipse((90, 90, 210, 210), fill=(40, 140, 70))
        # A near-white highlight ON the character: a global white key would
        # punch a hole here, which is why the fill is seeded from the corners.
        draw.ellipse((120, 120, 145, 145), fill=(252, 252, 252))
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _open(self, data):
        import io

        from PIL import Image

        return Image.open(io.BytesIO(data)).convert("RGBA")

    def test_the_backdrop_becomes_transparent(self) -> None:
        out = self._open(_strip_generated_backdrop(self._flat_character()))
        w, h = out.size
        corners = (out.getpixel((0, 0)), out.getpixel((w - 1, 0)),
                   out.getpixel((0, h - 1)), out.getpixel((w - 1, h - 1)))
        assert all(pixel[3] == 0 for pixel in corners)

    def test_the_character_survives_intact(self) -> None:
        out = self._open(_strip_generated_backdrop(self._flat_character()))
        w, h = out.size
        assert out.getpixel((w // 2, h // 2))[3] == 255

    def test_a_near_white_highlight_on_the_character_is_not_eaten(self) -> None:
        # The regression a global white threshold would cause.
        out = self._open(_strip_generated_backdrop(self._flat_character()))
        opaque = sum(1 for pixel in out.getdata() if pixel[3] > 0)
        assert opaque > 0
        highlight_kept = any(
            pixel[3] == 255 and min(pixel[:3]) > 240 for pixel in out.getdata()
        )
        assert highlight_kept

    def test_the_result_is_cropped_to_the_character(self) -> None:
        out = self._open(_strip_generated_backdrop(self._flat_character()))
        # Source was 300x300 with the subject inset; output should be tighter.
        assert max(out.size) < 300

    def test_unreadable_bytes_pass_through_untouched(self) -> None:
        # A portrait with a backdrop beats no portrait, so failure returns the
        # original rather than raising into the generation path.
        junk = b"not an image"
        assert _strip_generated_backdrop(junk) == junk


class TestRegenerationGate:
    """
    Whether a level-up should commission a NEW portrait.

    The decision must be keyed off stage_avatars, not avatar_stage.
    avatar_stage is written only by manual/evolution generation, so once
    look-ahead art exists it is stale — and a stale value made the gate fire
    for a form that had already been generated, billing a second image for art
    the user already owned.
    """

    @staticmethod
    def _should_regenerate(pet_prefs: dict, new_stage: int) -> bool:
        """Mirrors grant_xp's gate."""
        stage_avatars = pet_prefs.get("stage_avatars") or {}
        return bool(pet_prefs.get("avatar_url")) and not bool(stage_avatars.get(str(new_stage)))

    def test_a_user_with_no_portrait_is_never_asked_to_regenerate(self) -> None:
        # They are on Nowry by design; generating unprompted would both
        # surprise them and spend money they did not ask to spend.
        assert self._should_regenerate({}, 3) is False
        assert self._should_regenerate({"stage_avatars": {}}, 3) is False

    def test_a_stage_whose_art_already_exists_is_not_regenerated(self) -> None:
        # The look-ahead already paid for this form.
        prefs = {"avatar_url": "https://x/2.png", "stage_avatars": {"2": "https://x/2.png", "3": "https://x/3.png"}}
        assert self._should_regenerate(prefs, 3) is False

    def test_a_stage_with_no_art_yet_is_regenerated(self) -> None:
        prefs = {"avatar_url": "https://x/2.png", "stage_avatars": {"2": "https://x/2.png"}}
        assert self._should_regenerate(prefs, 3) is True

    def test_a_legacy_account_without_stage_avatars_still_regenerates(self) -> None:
        # Accounts that predate per-stage storage must not be stranded.
        assert self._should_regenerate({"avatar_url": "https://x/old.png"}, 4) is True

    def test_a_stale_avatar_stage_cannot_force_a_duplicate(self) -> None:
        # The exact regression: avatar_stage says 2, the user reaches 3, and
        # the look-ahead already generated 3. The old gate compared stages and
        # said yes; keying off the art itself says no.
        prefs = {
            "avatar_url": "https://x/2.png",
            "avatar_stage": 2,
            "stage_avatars": {"2": "https://x/2.png", "3": "https://x/3.png"},
        }
        assert prefs["avatar_stage"] < 3          # the stale comparison was true
        assert self._should_regenerate(prefs, 3) is False
