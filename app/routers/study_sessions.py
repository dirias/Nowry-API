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
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.firebase_auth import get_firebase_user
from app.config.database import decks_collection, study_cards_collection, study_sessions_collection
from app.models.study_sessions import LogSessionRequest, StudySessionRecord, StudySessionsResponse
from app.models.attempts import AttemptAfter, AttemptBefore, AttemptHelp, AttemptRecord, SessionAttemptsResponse
from app.services.attempts import deck_median_think_ms, session_attempts
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["study-sessions"])


# ---------------------------------------------------------------------------
# Naming a record (ADR-058 §1)
# ---------------------------------------------------------------------------


def _object_ids(values: list[str | None]) -> list[ObjectId]:
    """The ObjectIds among `values`; anything that is not one is skipped."""
    out: list[ObjectId] = []
    for value in values:
        if value and ObjectId.is_valid(str(value)):
            out.append(ObjectId(str(value)))
    return out


async def _deck_names(user_id: str, deck_ids: list[str | None]) -> dict[str, str]:
    """Deck id (as a string) → name, for this user's decks among `deck_ids`."""
    oids = _object_ids(deck_ids)
    if not oids:
        return {}
    decks = await decks_collection.find(
        {"_id": {"$in": oids}, "user_id": user_id},
        {"name": 1},
    ).to_list(length=len(oids))
    return {str(d["_id"]): d.get("name") for d in decks if d.get("name")}


async def _decks_of_cards(user_id: str, card_ids: list[str]) -> list[str]:
    """The distinct deck ids the cards belong to, in first-seen order."""
    oids = _object_ids(card_ids)
    if not oids:
        return []
    cards = await study_cards_collection.find(
        {"_id": {"$in": oids}, "user_id": user_id},
        {"deck_id": 1},
    ).to_list(length=len(oids))
    seen: list[str] = []
    for card in cards:
        deck_id = card.get("deck_id")
        if deck_id and str(deck_id) not in seen:
            seen.append(str(deck_id))
    return seen


async def _name_record(user_id: str, doc: dict) -> dict:
    """
    The fields a record needs to name itself, resolved from the data.

    Both clients used to read the deck's name off the first card and no cards
    route embeds a deck, so every record carried `deck_name: None` and every
    row read "Unknown topic". The server has the decks; it names the record.

    A deck session → `deck_name`. A cross-deck session (no `deck_id`) →
    `deck_count` and `deck_names`, read from the graded cards' decks. Returns
    only what is missing, so a caller can write exactly that back.
    """
    deck_id = doc.get("deck_id")
    if deck_id and ObjectId.is_valid(str(deck_id)):
        if doc.get("deck_name"):
            return {}
        names = await _deck_names(user_id, [deck_id])
        name = names.get(str(deck_id))
        return {"deck_name": name} if name else {}

    if doc.get("deck_count") is not None:
        return {}
    card_ids = [c.get("card_id") for c in doc.get("cards", []) if c.get("card_id")]
    deck_ids = await _decks_of_cards(user_id, card_ids)
    names = await _deck_names(user_id, deck_ids)
    missing: dict = {
        "deck_count": len(deck_ids),
        "deck_names": [names[d] for d in deck_ids if d in names],
    }
    # What the clients used to send for a daily review: the phone its route
    # sentinel as the deck id, the web an English literal as the name. Neither
    # is a deck, and a title string is not the client's to ship (ADR-058 §1).
    if deck_id:
        missing["deck_id"] = None
    if doc.get("deck_name"):
        missing["deck_name"] = None
    return missing


def _record(doc: dict) -> StudySessionRecord:
    return StudySessionRecord(
        id=str(doc["_id"]),
        session_type=doc.get("session_type", "ai_quiz"),
        topic=doc.get("topic"),
        deck_id=doc.get("deck_id"),
        deck_name=doc.get("deck_name"),
        deck_count=doc.get("deck_count"),
        deck_names=doc.get("deck_names") or [],
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

    # The phone's outbox is at-least-once: a log the server wrote but whose
    # response timed out is sent again. Same client id, same session (ADR-058 §2).
    if body.client_session_id:
        existing = await study_sessions_collection.find_one(
            {"user_id": user_id, "client_session_id": body.client_session_id},
            {"score_percentage": 1},
        )
        if existing:
            logger.info(f"[study_sessions] SRS repeat: user={user_id}, client_session_id={body.client_session_id}")
            return {
                "logged": True,
                "score_percentage": existing.get("score_percentage", 0),
                "session_id": str(existing["_id"]),
            }

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
    if body.client_session_id:
        doc["client_session_id"] = body.client_session_id
    # The record names itself here, not in the client (ADR-058 §1).
    doc.update(await _name_record(user_id, doc))

    inserted = await study_sessions_collection.insert_one(doc)
    logger.info(
        f"[study_sessions] SRS logged: user={user_id}, deck={doc.get('deck_name')!r}, "
        f"cards={total}, score={score_percentage}%, duration={duration_seconds}s"
    )
    # The id travels back so a study block can record the session it ran (PLAN-005).
    return {"logged": True, "score_percentage": score_percentage, "session_id": str(inserted.inserted_id)}


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
        # A record written before the server named them is completed the first
        # time it is read, and the result kept, so no migration has to be run
        # against any of the three databases (ADR-058 §1).
        if doc.get("session_type") in ("srs_review", "drill"):
            missing = await _name_record(user_id, doc)
            if missing:
                doc.update(missing)
                await study_sessions_collection.update_one({"_id": doc["_id"]}, {"$set": missing})
        sessions.append(_record(doc))

    logger.info(f"[study_sessions] Listed {len(sessions)}/{total} sessions for user={user_id}")
    return StudySessionsResponse(sessions=sessions, total=total)


# ---------------------------------------------------------------------------
# GET /v1/study-sessions/{session_id}/attempts  — the ledger behind one session (ADR-059)
# ---------------------------------------------------------------------------


def _attempt_record(row: dict) -> AttemptRecord:
    after = row.get("after")
    return AttemptRecord(
        id=str(row["_id"]),
        card_id=row.get("card_id", ""),
        card_title=row.get("card_title"),
        deck_id=row.get("deck_id"),
        tags=row.get("tags") or [],
        card_type=row.get("card_type") or "flashcard",
        grade=row.get("grade", "again"),
        evaluation=row.get("evaluation", "incorrect"),
        showing=row.get("showing") or "scheduled",
        side=row.get("side"),
        think_ms=row.get("think_ms"),
        judge_ms=row.get("judge_ms"),
        confidence=row.get("confidence"),
        chosen_option=row.get("chosen_option"),
        help=AttemptHelp(**(row.get("help") or {})),
        before=AttemptBefore(**(row.get("before") or {})),
        after=AttemptAfter(**after) if after else None,
        position=row.get("position"),
        total=row.get("total"),
        scope=row.get("scope"),
        client=row.get("client"),
        graded_at=row.get("graded_at", datetime.now(timezone.utc)),
    )


@router.get("/{session_id}/attempts", response_model=SessionAttemptsResponse)
async def get_session_attempts(
    session_id: str,
    current_user: dict = Depends(get_firebase_user),
) -> SessionAttemptsResponse:
    """
    The attempt rows behind one finished session, oldest first, with the
    learner's own median think time for the deck and how many days before
    this session the previous one ended. A session logged before the ledger
    existed has no rows: the clients keep their flat list for it.
    """
    user_id: str = current_user.get("user_id", "")
    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    session = await study_sessions_collection.find_one(
        {"_id": ObjectId(session_id), "user_id": user_id, "deleted_at": None},
        {"client_session_id": 1, "deck_id": 1, "started_at": 1, "completed_at": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    client_session_id = session.get("client_session_id")
    rows = await session_attempts(user_id, client_session_id) if client_session_id else []
    median_ms = await deck_median_think_ms(user_id, session.get("deck_id")) if rows else None

    started = session.get("started_at") or session.get("completed_at")
    previous = await study_sessions_collection.find_one(
        {"user_id": user_id, "deleted_at": None, "completed_at": {"$lt": started}},
        {"completed_at": 1},
        sort=[("completed_at", -1)],
    ) if started else None
    days_since_previous = None
    if previous and previous.get("completed_at") and started:
        gap = started.replace(tzinfo=None) - previous["completed_at"].replace(tzinfo=None)
        days_since_previous = max(0, gap.days)

    return SessionAttemptsResponse(
        attempts=[_attempt_record(r) for r in rows],
        median_think_ms=median_ms,
        days_since_previous=days_since_previous,
    )
