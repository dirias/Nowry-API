"""
Phase 15 — Goal model tests (CAL-02).
Verifies Milestone sub-model structure and Pydantic v2 retroactive field defaults.
"""
import pytest
from app.models.Goal import Goal, Milestone


def test_milestone_model():
    """Milestone sub-model has correct fields and defaults (D-06)."""
    m = Milestone(title="Q1 Planning")
    assert m.title == "Q1 Planning"
    assert m.due_date is None
    assert m.completed is False
    assert m.is_key_result is False

    kr = Milestone(title="Revenue KR", due_date="2026-03-31", is_key_result=True)
    assert kr.is_key_result is True
    assert kr.due_date == "2026-03-31"


def test_goal_milestone_deserialization():
    """
    Deserializing a Goal from a dict where milestones lack is_key_result
    applies Pydantic v2 default (False) — no migration script needed (D-07).
    """
    raw_goal = {
        "_id": "000000000000000000000000",
        "focus_area_id": "area1",
        "title": "Grow Revenue",
        "milestones": [
            {"title": "Q1 Planning", "due_date": "2026-03-31"},
            {"title": "Revenue KR", "due_date": "2026-06-30", "is_key_result": True},
        ],
    }
    goal = Goal(**raw_goal)
    assert len(goal.milestones) == 2
    # First milestone: no is_key_result in source → Pydantic fills default False
    assert goal.milestones[0].is_key_result is False
    # Second milestone: explicit True preserved
    assert goal.milestones[1].is_key_result is True
    # Both are Milestone instances (not plain dicts)
    assert isinstance(goal.milestones[0], Milestone)
    assert isinstance(goal.milestones[1], Milestone)
