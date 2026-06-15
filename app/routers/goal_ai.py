from __future__ import annotations
import asyncio
import json
import re
import logging
from contextlib import ExitStack
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
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
from app.core.evaluation_helper import score_trace
from app.core.langfuse_client import get_langfuse_client
from langfuse import propagate_attributes

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


def _parse_goal_analysis(raw_text: str) -> tuple[GoalAnalysisResponse, Optional[Exception]]:
    """Parse a Gemini JSON response into a GoalAnalysisResponse.

    Returns (response, error). error is None on success. On failure (malformed
    JSON, missing/wrong-typed fields, a non-dict top-level JSON value, or a
    syntactically-valid-but-schema-mismatched payload), response is the
    soft-failure GoalAnalysisResponse([], [], []) and error is the
    json.JSONDecodeError / KeyError / TypeError / AttributeError / pydantic
    ValidationError that was caught.
    """
    try:
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            raise TypeError(f"Expected a JSON object, got {type(data).__name__}")
        return (
            GoalAnalysisResponse(
                suggestions=[GoalSuggestion(**s) for s in data.get("suggestions", [])],
                conflicts=[GoalConflict(**c) for c in data.get("conflicts", [])],
                archiving_recommendations=[
                    ArchivingRecommendation(**r)
                    for r in data.get("archiving_recommendations", [])
                ],
            ),
            None,
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValidationError) as exc:
        return (
            GoalAnalysisResponse(suggestions=[], conflicts=[], archiving_recommendations=[]),
            exc,
        )


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

    client = get_langfuse_client()
    model_name = "models/gemini-pro-latest"  # Goal AI is Pro-only (D-06)
    trace_metadata = {
        "feature": "goal_ai",
        "tier": "pro",
        "user_id": str(user_id),
        "model": model_name,
    }

    if client:
        # Phase 1: Langfuse instrumentation setup ONLY (propagate_attributes +
        # start_as_current_observation). The actual Gemini call is deliberately
        # OUTSIDE this try/except -- a genuine LLM failure must never be
        # misattributed as "Langfuse tracing failed" nor trigger a duplicate
        # paid call (WR-04).
        propagate_stack = ExitStack()
        generation_stack = ExitStack()
        generation = None
        try:
            propagate_stack.enter_context(
                propagate_attributes(
                    user_id=str(user_id),
                    trace_name="goal_ai",
                    metadata=trace_metadata,
                    tags=["goal_ai", "pro"],
                )
            )
            generation = generation_stack.enter_context(
                client.start_as_current_observation(
                    name="goal_ai",
                    as_type="generation",
                    model=model_name,
                    input=[{"role": "user", "content": combined_prompt}],
                )
            )
        except Exception as langfuse_exc:
            logger.warning(
                f"goal_ai: Langfuse tracing failed, continuing without trace: {langfuse_exc}"
            )
            generation_stack.close()
            propagate_stack.close()
            generation = None

        try:
            # Phase 2: the actual LLM call + response extraction. Not covered by
            # the Langfuse except above -- a real Gemini error propagates as-is
            # (unhandled 500), matching the untraced/disabled-client paths below,
            # instead of being logged as a Langfuse failure and re-issuing a
            # second paid call.
            response = await asyncio.to_thread(_gemini_client.request, combined_prompt)
            raw_text = response.choices[0].message.content
            raw_text = re.sub(r"^```json\n?|```$", "", raw_text.strip(), flags=re.MULTILINE)

            if generation is not None:
                try:
                    generation.update(output=raw_text, usage_details=None)
                except Exception as langfuse_exc:
                    logger.warning(
                        f"goal_ai: Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                    )
        finally:
            generation_stack.close()

        # D-07: format-valid scoring -- still inside propagate_attributes
        # (closed below), outside the closed start_as_current_observation span.
        # score_trace() is fire-and-forget and never raises; if Langfuse setup
        # failed above, it silently no-ops (matching the prior
        # untraced-fallback behavior, which never called score_trace).
        try:
            result, parse_err = _parse_goal_analysis(raw_text)
            if generation is not None:
                if parse_err is None:
                    # D-04: no comment on success
                    score_trace(name="format-valid", value=True)
                else:
                    # D-03: truncated error + raw-output snippet
                    snippet = raw_text[:300]
                    score_trace(
                        name="format-valid",
                        value=False,
                        comment=f"{parse_err}\nRaw output (truncated): {snippet}",
                    )
            if parse_err is not None:
                logger.warning("goal_ai: Failed to parse Gemini response: %s", parse_err)
        finally:
            propagate_stack.close()
        # D-07: existing soft-failure behavior unchanged on error
        return result
    else:
        # client is None -- Langfuse disabled (untraced path, identical to pre-Phase-13 behavior)
        response = await asyncio.to_thread(_gemini_client.request, combined_prompt)
        raw_text = re.sub(
            r"^```json\n?|```$", "", response.choices[0].message.content.strip(), flags=re.MULTILINE
        )
        result, parse_err = _parse_goal_analysis(raw_text)
        if parse_err is not None:
            logger.warning("goal_ai: Failed to parse Gemini response: %s", parse_err)
        return result
