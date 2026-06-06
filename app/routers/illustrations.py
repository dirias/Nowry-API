"""
Illustration Magic — POST /book/{book_id}/diagram

Wraps the existing visualizer pipeline with book ownership check and per-book
illustration counter for Free-tier gating (D-10). Plus and Pro have no cap.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.dependencies import track_ai_usage
from app.auth.firebase_auth import get_firebase_user
from app.config.database import books_collection
from app.models.illustration import DiagramRequest, DiagramResponse
from app.utils.logger import get_logger
from bson import ObjectId

logger = get_logger(__name__)

router = APIRouter(
    prefix="/book",
    tags=["illustrations"],
    dependencies=[Depends(get_firebase_user)],
)

_FREE_TIER_CAP = 2  # Max diagrams per book for Free tier (D-10)


@router.post("/{book_id}/diagram", response_model=DiagramResponse)
async def generate_diagram(
    book_id: str,
    body: DiagramRequest,
    current_user: dict = Depends(track_ai_usage),
) -> DiagramResponse:
    """Generate a Mermaid diagram from selected book text.

    Free tier: capped at 2 diagrams per book (tracked in MongoDB).
    Plus/Pro: no per-book cap.
    """
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    user_id: str = current_user.get("user_id", "")

    # Ownership check (T-6-04)
    try:
        book = await books_collection.find_one(
            {"_id": ObjectId(book_id), "deleted_at": None}
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")

    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    # Free-tier cap check — block LLM call when already at cap (cost protection)
    if tier == "free":
        count = book.get("illustration_count", 0)
        if count >= _FREE_TIER_CAP:
            raise HTTPException(
                status_code=403,
                detail="Illustration limit reached. Upgrade to Plus for unlimited diagrams.",
            )

    # Counter is NOT incremented here — it increments only when the user
    # confirms insertion via POST /book/{id}/diagram/confirm (count-on-insert).

    # Map diagram_type "auto" to a sensible default for the visualizer pipeline
    viz_type = body.diagram_type if body.diagram_type != "auto" else "mindmap"

    inputs = {
        "text": body.selected_text,
        "viz_type": viz_type,
        "tier": tier,
    }

    try:
        result = orchestrator.invoke("visualizer", inputs)  # synchronous — NO await
    except Exception as exc:
        logger.error(f"[illustrations] LLM error for book={book_id} tier={tier}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])

    return DiagramResponse(
        mermaid_code=result.get("mermaid_code", ""),
        explanation=result.get("explanation", ""),
    )


@router.post("/{book_id}/diagram/confirm", status_code=200)
async def confirm_diagram_insert(
    book_id: str,
    current_user: dict = Depends(track_ai_usage),
) -> dict:
    """Atomically increment the illustration counter when the user inserts a
    diagram into their book. Returns 403 if the Free-tier cap is already reached
    (race condition guard — the generate endpoint also checks, but a concurrent
    session could have filled the last slot between generate and insert).
    """
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    user_id: str = current_user.get("user_id", "")

    try:
        book = await books_collection.find_one(
            {"_id": ObjectId(book_id), "deleted_at": None}
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")

    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    if tier == "free":
        count = book.get("illustration_count", 0)
        if count >= _FREE_TIER_CAP:
            raise HTTPException(
                status_code=403,
                detail="Illustration limit reached. Upgrade to Plus for unlimited diagrams.",
            )

    await books_collection.update_one(
        {"_id": ObjectId(book_id)},
        {"$inc": {"illustration_count": 1}},
    )
    return {"ok": True}
