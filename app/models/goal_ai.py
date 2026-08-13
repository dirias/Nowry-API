from __future__ import annotations
from pydantic import BaseModel
from typing import List, Optional


class GoalSuggestion(BaseModel):
    """AI-generated quarterly goal breakdown with milestone timeline. (GOAL-01)"""
    goal_title: str
    quarter: int                # 1, 2, 3, or 4
    milestones: List[str]       # ["Complete market research by Mar 15", ...]
    rationale: str              # "Q1 is lightly loaded — good time to start this goal"


class GoalConflict(BaseModel):
    """AI-detected inconsistency or over-commitment. (GOAL-02)"""
    description: str            # "Q2 has 9 goals — typical capacity is 3-5"
    affected_goals: List[str]   # ["Run 5K", "Learn Spanish", ...]


class ArchivingRecommendation(BaseModel):
    """AI recommendation to archive a stale or completed goal. (GOAL-03)"""
    goal_title: str
    reason: str                 # "Stale: 0% progress, migrated 3 times, no target_date set"


class GoalAnalysisResponse(BaseModel):
    suggestions: List[GoalSuggestion]
    conflicts: List[GoalConflict]
    archiving_recommendations: List[ArchivingRecommendation]


class GoalAnalysisRequest(BaseModel):
    year: Optional[int] = None  # defaults to datetime.now().year in router
