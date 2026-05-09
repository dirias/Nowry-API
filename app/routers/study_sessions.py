"""
Study Sessions Router — persistent session history.

Endpoints:
  GET /v1/study-sessions  — Return the authenticated user's completed quiz
                            sessions in reverse-chronological order.

Sessions are written to the permanent `study_sessions` collection by the
quiz answer handler whenever a session reaches `status=complete`. They are
never expired (no TTL index) and form the foundation for future analytics.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from app.auth.firebase_auth import get_firebase_user
from app.config.database import study_sessions_collection
from app.models.study_sessions import LogSessionRequest, StudySessionRecord, StudySessionsResponse
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["study-sessions"])


# ---------------------------------------------------------------------------
# POST /v1/study-sessions/log  — SRS review session completion
# ---------------------------------------------------------------------------


@router.post("/log", status_code=201)
async def log_study_session(
    body: LogSessionRequest,
    current_user: dict = Depends(get_firebase_user),
) -> dict:
    """
    Persist a completed SRS flashcard review session.

    Called by the frontend when StudySession.js reaches sessionComplete.
    The frontend tracks every graded card; this endpoint computes totals
    and writes the permanent record to study_sessions.

    Grade → evaluation mapping (mirrors SM-2 quality scores):
      again → incorrect
      hard  → partial
      good  → correct
      easy  → correct
    """
    user_id: str = current_user.get("user_id", "")

    cards_data: list[dict] = [
        {
            "card_id": c.card_id,
            "card_title": c.card_title,
            "grade": c.grade,
            "evaluation": c.evaluation,
        }
        for c in body.cards
    ]

    total: int = len(cards_data)
    correct: int = sum(1 for c in body.cards if c.evaluation == "correct")
    partial: int = sum(1 for c in body.cards if c.evaluation == "partial")
    incorrect: int = total - correct - partial
    score_percentage: int = round((correct + partial * 0.5) / total * 100) if total > 0 else 0

    now: datetime = datetime.now(timezone.utc)
    duration_seconds: int = max(0, int((now - body.started_at).total_seconds()))

    doc: dict = {
        "user_id": user_id,
        "session_type": body.session_type,
        "topic": None,
        "deck_id": body.deck_id,
        "deck_name": body.deck_name,
        "total_cards": total,
        "correct": correct,
        "partial": partial,
        "incorrect": incorrect,
        "score_percentage": score_percentage,
        "duration_seconds": duration_seconds,
        "cards": cards_data,
        "started_at": body.started_at,
        "completed_at": now,
    }

    await study_sessions_collection.insert_one(doc)
    logger.info(
        f"[study_sessions] SRS logged: user={user_id}, deck={body.deck_name!r}, "
        f"cards={total}, score={score_percentage}%, duration={duration_seconds}s"
    )
    return {"logged": True, "score_percentage": score_percentage}


# ---------------------------------------------------------------------------
# GET /v1/study-sessions
# ---------------------------------------------------------------------------


@router.get("", response_model=StudySessionsResponse)
async def list_study_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: dict = Depends(get_firebase_user),
) -> StudySessionsResponse:
    """
    Return the user's completed study sessions, newest first.

    - `limit` controls how many records are returned (default 20, max 100).
    - Each record includes per-card evaluations for drill-down history.
    """
    user_id: str = current_user.get("user_id", "")

    total: int = await study_sessions_collection.count_documents({"user_id": user_id, "deleted_at": None})

    raw_docs = await (
        study_sessions_collection
        .find({"user_id": user_id, "deleted_at": None})
        .sort("completed_at", -1)
        .to_list(length=limit)
    )

    sessions: list[StudySessionRecord] = []
    for doc in raw_docs:
        sessions.append(
            StudySessionRecord(
                id=str(doc["_id"]),
                session_type=doc.get("session_type", "ai_quiz"),
                topic=doc.get("topic"),
                deck_id=doc.get("deck_id"),
                deck_name=doc.get("deck_name"),
                total_cards=doc.get("total_cards", 0),
                correct=doc.get("correct", 0),
                partial=doc.get("partial", 0),
                incorrect=doc.get("incorrect", 0),
                score_percentage=doc.get("score_percentage", 0),
                duration_seconds=doc.get("duration_seconds", 0),
                cards=doc.get("cards", []),
                started_at=doc.get("started_at", doc.get("completed_at", datetime.now(timezone.utc))),
                completed_at=doc.get("completed_at", datetime.now(timezone.utc)),
            )
        )

    logger.info(f"[study_sessions] Listed {len(sessions)}/{total} sessions for user={user_id}")
    return StudySessionsResponse(sessions=sessions, total=total)
