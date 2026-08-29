"""
Tests for the Study Pet XP economy (PET-003).

These lock two different things:

1. The *curve mechanics* — level/XP round-tripping, monotonicity, stage
   mapping. Ordinary correctness.

2. The *economy balance* — how long the six evolution stages take to reach,
   and how the reward sources compare to each other. This is the half that
   actually broke: the original constants took ~700 days to reach the final
   stage, and a task completion was worth 25 card reviews. Neither is a bug
   any type checker or unit test of the maths would have caught, so the
   balance invariants are asserted explicitly here.
"""
import math
import sys
from unittest.mock import MagicMock

import pytest

# app.routers.agent imports the Gemini SDK at module scope purely for the chat
# endpoint, none of which the pure XP functions below touch. Stub the
# subpackages that are not installed in this Python 3.9 dev venv, matching the
# precedent in test_tracing.py's _ensure_agent_importable().
for _mod in (
    "google.generativeai",
    "google.generativeai.types",
    "google.generativeai.protos",
    "google.api_core.exceptions",
):
    sys.modules.setdefault(_mod, MagicMock())

from app.routers.agent import (  # noqa: E402
    CHAT_XP_MESSAGES_PER_DAY,
    STAGE_LEVEL_THRESHOLDS,
    XP_LEVEL_DIVISOR,
    XP_PER_CARD_REVIEW,
    XP_PER_CHAT_MESSAGE,
    XP_PER_DAILY_STREAK,
    XP_PER_SESSION_COMPLETE,
    XP_PER_TASK_COMPLETE,
    _calculate_level,
    _level_to_stage,
    _xp_for_level,
    _xp_for_next_level,
)

# A "regular learner": 30 card reviews, one finished session, one streak bonus.
REGULAR_DAY_XP = 30 * XP_PER_CARD_REVIEW + XP_PER_SESSION_COMPLETE + XP_PER_DAILY_STREAK

# A "light learner": 10 card reviews, one finished session, one streak bonus.
LIGHT_DAY_XP = 10 * XP_PER_CARD_REVIEW + XP_PER_SESSION_COMPLETE + XP_PER_DAILY_STREAK


# ---------------------------------------------------------------------------
# Curve mechanics
# ---------------------------------------------------------------------------


class TestCurveMechanics:
    def test_level_and_xp_for_level_round_trip(self) -> None:
        for level in range(1, 40):
            assert _calculate_level(_xp_for_level(level)) == level

    def test_level_never_decreases_as_xp_grows(self) -> None:
        previous = 1
        for xp in range(0, 30_000, 7):
            current = _calculate_level(xp)
            assert current >= previous
            previous = current

    def test_level_floors_at_one_for_zero_and_negative_xp(self) -> None:
        assert _calculate_level(0) == 1
        assert _calculate_level(-500) == 1

    def test_xp_for_next_level_lands_exactly_on_the_next_threshold(self) -> None:
        for xp in range(0, 8_000, 13):
            remaining = _xp_for_next_level(xp)
            assert remaining > 0
            assert _calculate_level(xp + remaining) == _calculate_level(xp) + 1

    def test_stage_spans_one_to_six_and_never_decreases(self) -> None:
        stages = [_level_to_stage(level) for level in range(1, 60)]
        assert stages == sorted(stages)
        assert min(stages) == 1
        assert max(stages) == 6

    def test_stage_boundaries_match_the_declared_thresholds(self) -> None:
        assert _level_to_stage(1) == 1
        for index, threshold in enumerate(STAGE_LEVEL_THRESHOLDS):
            expected = index + 2
            assert _level_to_stage(threshold) == expected
            assert _level_to_stage(threshold - 1) == expected - 1

    def test_stage_thresholds_are_strictly_increasing(self) -> None:
        assert list(STAGE_LEVEL_THRESHOLDS) == sorted(set(STAGE_LEVEL_THRESHOLDS))
        assert len(STAGE_LEVEL_THRESHOLDS) == 5  # stages 2..6

    def test_curve_matches_its_documented_closed_form(self) -> None:
        for xp in (0, 1, 24, 25, 100, 624, 625, 2_025, 7_225):
            assert _calculate_level(xp) == max(
                1, math.floor(math.sqrt(max(0, xp) / XP_LEVEL_DIVISOR)) + 1
            )


# ---------------------------------------------------------------------------
# Economy balance — the half that actually regressed
# ---------------------------------------------------------------------------


class TestEconomyBalance:
    def _days_to_stage(self, stage: int, xp_per_day: int) -> float:
        """Days for a learner earning `xp_per_day` to reach `stage`."""
        level = 1 if stage == 1 else STAGE_LEVEL_THRESHOLDS[stage - 2]
        return _xp_for_level(level) / xp_per_day

    def test_regular_learner_reaches_the_final_stage_within_a_quarter(self) -> None:
        # The original curve put this at ~700 days, i.e. five of the six
        # evolutions sat beyond any plausible retention window.
        assert self._days_to_stage(6, REGULAR_DAY_XP) <= 100

    def test_regular_learner_sees_their_first_evolution_on_day_one(self) -> None:
        # The first evolution has to land inside the first session or two, or
        # a new user never learns the pet evolves at all.
        assert self._days_to_stage(2, REGULAR_DAY_XP) <= 2

    def test_every_stage_is_reachable_inside_a_retention_window(self) -> None:
        for stage in range(2, 7):
            assert self._days_to_stage(stage, REGULAR_DAY_XP) <= 100

    def test_even_a_light_learner_finishes_the_arc_within_a_year(self) -> None:
        assert self._days_to_stage(6, LIGHT_DAY_XP) <= 365

    def test_stages_stay_spread_out_rather_than_bunching_up_front(self) -> None:
        # Guards the opposite failure: a curve so fast the whole arc is spent
        # in the first week and there is nothing left to earn.
        assert self._days_to_stage(6, REGULAR_DAY_XP) >= 30

    def test_a_task_is_not_worth_more_than_a_handful_of_card_reviews(self) -> None:
        # Was 50 XP against a review's 2, i.e. one checkbox = 25 reviews.
        reviews_per_task = XP_PER_TASK_COMPLETE / XP_PER_CARD_REVIEW
        assert reviews_per_task <= 10

    def test_chatting_all_day_cannot_outearn_one_study_session(self) -> None:
        chat_ceiling = CHAT_XP_MESSAGES_PER_DAY * XP_PER_CHAT_MESSAGE
        session_value = 30 * XP_PER_CARD_REVIEW + XP_PER_SESSION_COMPLETE
        assert chat_ceiling < session_value

    def test_finishing_a_session_beats_a_single_card_but_not_a_whole_one(self) -> None:
        assert XP_PER_SESSION_COMPLETE > XP_PER_CARD_REVIEW
        assert XP_PER_SESSION_COMPLETE < 30 * XP_PER_CARD_REVIEW

    @pytest.mark.parametrize(
        "amount",
        [
            XP_PER_CARD_REVIEW,
            XP_PER_SESSION_COMPLETE,
            XP_PER_DAILY_STREAK,
            XP_PER_TASK_COMPLETE,
            XP_PER_CHAT_MESSAGE,
        ],
    )
    def test_every_reward_is_a_positive_integer(self, amount: int) -> None:
        assert isinstance(amount, int)
        assert amount > 0
