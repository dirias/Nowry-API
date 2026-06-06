from __future__ import annotations
import asyncio
import json
import re
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from app.auth.dependencies import get_subscription_tier, track_ai_usage
from app.config.database import (
    annual_plans_collection,
    focus_areas_collection,
    priorities_collection,
    goals_collection,
)
from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client
from app.models.goal_ai import (
    GoalAnalysisRequest,
    GoalAnalysisResponse,
    GoalSuggestion,
    GoalConflict,
    ArchivingRecommendation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/goal-ai", tags=["goal-ai"])

_gemini_client = Gemini_client("models/gemini-pro-latest")

_SYSTEM_PROMPT = """You are an expert annual planning coach. Analyze the user's annual plan and return a JSON object with exactly three fields:
- "suggestions": list of quarterly goal suggestions, each with keys: goal_title (str), quarter (int 1-4), milestones (list of str), rationale (str)
- "conflicts": list of detected conflicts or over-commitments, each with keys: description (str), affected_goals (list of str)
- "archiving_recommendations": list of stale or completed goals to archive, each with keys: goal_title (str), reason (str)

Return ONLY valid JSON. No markdown fences, no explanation text."""


def _serialize_plan(areas: list, priorities: list, goal_lists: list) -> str:
    """Compact plan summary for the AI prompt."""
    lines = ["=== ANNUAL PLAN ===", f"Focus areas: {len(areas)}"]
    for area, goals in zip(areas, goal_lists):
        lines.append(f"\nFocus Area: {area.get('name', 'Unnamed')}")
        for g in goals:
            ms_count = len(g.get("milestones", []))
            lines.append(
                f"  - [{g.get('quarter', '?')}] {g.get('title', '')} "
                f"| status={g.get('status', 'unknown')} progress={g.get('progress', 0)}% "
                f"target={g.get('target_date', 'none')} migrations={g.get('migration_count', 0)} "
                f"milestones={ms_count}"
            )
    lines.append(f"\nPriorities ({len(priorities)}):")
    for p in priorities:
        lines.append(
            f"  - {p.get('title', '')} deadline={p.get('deadline', 'none')} "
            f"completed={p.get('is_completed', False)}"
        )
    return "\n".join(lines)


@router.post("/analyze", response_model=GoalAnalysisResponse)
async def analyze_goals(
    body: GoalAnalysisRequest = GoalAnalysisRequest(),
    current_user: dict = Depends(track_ai_usage),
    tier: str = Depends(get_subscription_tier),
) -> GoalAnalysisResponse:
    """Analyze the user's annual plan and return AI-generated suggestions, conflicts, and archiving recommendations.
    Pro-only endpoint (per D-06). Reads annual plan from MongoDB directly (no HTTP call to /annual-plan/full).
    """
    if tier != "pro":
        raise HTTPException(
            status_code=403,
            detail="Goal AI analysis requires a Pro subscription.",
        )

    user_id = current_user.get("user_id")
    target_year = body.year if body.year else datetime.now(timezone.utc).year

    # Read annual plan (soft-delete aware)
    plan = await annual_plans_collection.find_one(
        {"user_id": user_id, "year": target_year, "deleted_at": None}
    )
    if not plan:
        raise HTTPException(
            status_code=404,
            detail="No annual plan found. Create a plan first.",
        )

    plan_id = str(plan["_id"])

    # Parallel fetch of plan data (mirrors get_full_annual_plan pattern)
    areas, priorities = await asyncio.gather(
        focus_areas_collection.find(
            {"annual_plan_id": plan_id, "deleted_at": None}
        ).to_list(length=10),
        priorities_collection.find(
            {"annual_plan_id": plan_id, "deleted_at": None}
        ).to_list(length=50),
    )
    goal_lists = await asyncio.gather(
        *[
            goals_collection.find(
                {"focus_area_id": str(a["_id"]), "deleted_at": None}
            ).to_list(length=100)
            for a in areas
        ]
    )

    plan_summary = _serialize_plan(areas, priorities, goal_lists)
    combined_prompt = f"{_SYSTEM_PROMPT}\n\n{plan_summary}"

    try:
        response = _gemini_client.request(combined_prompt)
        raw_text = response.choices[0].message.content
        # Strip markdown fences if present
        raw_text = re.sub(r"^```json\n?|```$", "", raw_text.strip(), flags=re.MULTILINE)
        data = json.loads(raw_text)
        return GoalAnalysisResponse(
            suggestions=[GoalSuggestion(**s) for s in data.get("suggestions", [])],
            conflicts=[GoalConflict(**c) for c in data.get("conflicts", [])],
            archiving_recommendations=[
                ArchivingRecommendation(**r)
                for r in data.get("archiving_recommendations", [])
            ],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("goal_ai: Failed to parse Gemini response: %s", exc)
        return GoalAnalysisResponse(suggestions=[], conflicts=[], archiving_recommendations=[])
