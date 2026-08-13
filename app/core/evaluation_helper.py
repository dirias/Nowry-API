"""
Thin wrapper around Langfuse's scoring API (langfuse>=4.7.0).

Exposes score_trace(trace_id, name, value, comment) so feature code never
imports the Langfuse SDK directly for scoring. Follows the Phase 9/12
house convention: if client: ... guarded, fire-and-forget, WARNING-only
on failure -- NEVER raises, NEVER blocks the caller.

Usage (current-trace path -- used by text_node.py, quiz_node.py,
visualizer_node.py, goal_ai.py -- all run inside an active
propagate_attributes/start_as_current_observation context):

    from app.core.evaluation_helper import score_trace
    score_trace(name="format-valid", value=True)

Usage (explicit-trace path -- for future out-of-band/batch scoring,
satisfies EV-02's literal signature):

    score_trace(trace_id="abc123...", name="format-valid", value=False,
                 comment="JSONDecodeError: ...")
"""

import logging
from typing import Optional, Union

from app.core.langfuse_client import get_langfuse_client

logger = logging.getLogger(__name__)


# Map Python value types to Langfuse's ScoreDataType ("NUMERIC" | "CATEGORICAL"
# | "BOOLEAN" | "TEXT"). bool MUST be checked before int/float -- in Python,
# `isinstance(True, int)` is True, so the bool branch must come first.
def _infer_data_type(value: Union[bool, int, float, str]) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, (int, float)):
        return "NUMERIC"
    return "CATEGORICAL"


def score_trace(
    trace_id: Optional[str] = None,
    *,
    name: str,
    value: Union[bool, int, float, str],
    comment: Optional[str] = None,
) -> None:
    """
    Attach a quality score to a Langfuse trace.

    Args:
        trace_id: If provided, scores that specific trace via create_score()
            (out-of-band/batch path). If None, scores the CURRENT active
            trace via score_current_trace() -- requires this function to be
            called from within an active propagate_attributes /
            start_as_current_observation context.
        name: Score name (e.g., "format-valid"). MUST be the literal,
            uniform name across all call sites -- do not suffix per feature.
        value: bool -> BOOLEAN, int/float -> NUMERIC, str -> CATEGORICAL.
        comment: Optional human-readable detail (e.g., truncated
            JSONDecodeError + raw-output snippet). None on success.

    Never raises. Logs at WARNING and returns silently on any failure,
    including: Langfuse disabled (client is None, silent), no active trace
    context when trace_id is None, or any SDK/network error.
    """
    client = get_langfuse_client()
    if not client:
        return  # Langfuse disabled (Phase 9 D-08) -- silent no-op, no warning

    data_type = _infer_data_type(value)

    try:
        if trace_id is not None:
            client.create_score(
                trace_id=trace_id,
                name=name,
                value=value,
                data_type=data_type,  # type: ignore[arg-type]
                comment=comment,
            )
        else:
            client.score_current_trace(
                name=name,
                value=value,
                data_type=data_type,  # type: ignore[arg-type]
                comment=comment,
            )
    except Exception as langfuse_exc:
        # Fire-and-forget per Phase 9 D-08 / Phase 12 D-07 -- never raises,
        # never blocks the user-facing response. WARNING makes missing
        # scores diagnosable.
        logger.warning(
            "score_trace failed for name=%r trace_id=%r: %s",
            name,
            trace_id,
            langfuse_exc,
        )
