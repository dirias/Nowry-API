"""
Agent Tools — Read-Only Knowledge Access for the Study Buddy

This module provides the database-level retrieval functions that the Gemini
Function Calling mechanism will invoke on demand. All functions are:
  1. Read-only — no writes, ever.
  2. Scoped to a specific user_id — no cross-user data leakage is possible.
  3. Designed to be lightweight — they return structured summaries, not raw
     database documents, to keep token usage predictable.

The functions here are registered as Gemini Tools and called autonomously by
the model when it decides they are relevant to the user's question.
"""

import json
import re
from datetime import datetime, timezone
from bson import ObjectId

from app.config.database import (
    books_collection,
    cards_collection,
    decks_collection,
    focus_areas_collection,
    goals_collection,
    annual_plans_collection,
)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _lexical_to_plaintext(lexical_json_str: str, max_chars: int = 4000) -> str:
    """
    Convert a Lexical JSON string (rich text editor format) into plain text
    suitable for LLM consumption. Strips all structural metadata and returns
    only the textual content, capped at max_chars to control token usage.
    """
    if not lexical_json_str:
        return ""

    try:
        data = json.loads(lexical_json_str)
    except (json.JSONDecodeError, TypeError):
        # If it's already plain text or broken JSON, just truncate it
        return str(lexical_json_str)[:max_chars]

    texts: list[str] = []

    def _walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        # Extract text from text nodes
        if node_type == "text":
            text = node.get("text", "").strip()
            if text:
                texts.append(text)
        # Add paragraph breaks for block-level nodes
        elif node_type in ("paragraph", "heading", "quote"):
            for child in node.get("children", []):
                _walk(child)
            texts.append("\n")
        else:
            for child in node.get("children", []):
                _walk(child)

    root = data.get("root", data)
    _walk(root)

    plaintext = " ".join(texts)
    # Collapse multiple whitespace/newlines
    plaintext = re.sub(r"\n{3,}", "\n\n", plaintext)
    plaintext = re.sub(r" {2,}", " ", plaintext)
    return plaintext[:max_chars].strip()


# ---------------------------------------------------------------------------
# Study & Card Tools
# ---------------------------------------------------------------------------

async def get_study_summary(user_id: str) -> dict:
    """
    Returns a high-level summary of the user's current study status:
    total cards, due cards, new cards, reviewed cards, and active decks.
    Used by the agent to answer "What should I study?" or "How am I doing?".
    """
    now = datetime.now(timezone.utc)

    # Count cards in active (non-deleted) decks
    active_decks = await decks_collection.find(
        {"user_id": user_id, "deleted_at": None}, {"_id": 1, "name": 1}
    ).to_list(length=200)

    deck_ids = []
    deck_names = []
    for d in active_decks:
        deck_ids.append(d["_id"])
        deck_ids.append(str(d["_id"]))
        deck_names.append(d.get("name", "Unnamed Deck"))

    base_filter = {
        "user_id": user_id,
        "deleted_at": None,
        "$or": [{"deck_id": None}, {"deck_id": {"$in": deck_ids}}] if deck_ids else [{"deck_id": None}],
    }

    total_cards = await cards_collection.count_documents(base_filter)

    reviewed_cards = await cards_collection.count_documents(
        {**base_filter, "last_reviewed": {"$ne": None}}
    )

    new_cards = total_cards - reviewed_cards

    due_cards = await cards_collection.count_documents(
        {
            **base_filter,
            "last_reviewed": {"$ne": None},
            "next_review": {"$lte": now},
        }
    )

    # Deck-level summary: name + due count per deck
    deck_summaries = []
    for d in active_decks:
        oid = d["_id"]
        deck_filter = {"deck_id": {"$in": [oid, str(oid)]}, "deleted_at": None}
        due = await cards_collection.count_documents(
            {**deck_filter, "last_reviewed": {"$ne": None}, "next_review": {"$lte": now}}
        )
        new = await cards_collection.count_documents(
            {**deck_filter, "last_reviewed": None}
        )
        if due > 0 or new > 0:
            deck_summaries.append({
                "deck_name": d.get("name", "Unnamed"),
                "due_cards": due,
                "new_cards": new,
            })

    return {
        "total_cards": total_cards,
        "reviewed_cards": reviewed_cards,
        "new_cards": new_cards,
        "due_cards": due_cards,
        "active_decks": len(active_decks),
        "decks_with_pending_work": deck_summaries,
        "recommendation": (
            f"You have {due_cards} cards due for review and {new_cards} new cards to learn."
            if due_cards > 0 or new_cards > 0
            else "You're all caught up! Great work."
        ),
    }


async def list_decks(user_id: str) -> list[dict]:
    """
    Returns a lightweight list of all the user's decks: name, card count,
    and mastery percentage. Used when the user asks about their collections.
    """
    now = datetime.now(timezone.utc)
    decks = await decks_collection.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 1, "name": 1, "description": 1, "tags": 1}
    ).to_list(length=100)

    result = []
    for d in decks:
        oid = d["_id"]
        deck_filter = {"deck_id": {"$in": [oid, str(oid)]}, "deleted_at": None}
        total = await cards_collection.count_documents(deck_filter)
        reviewed = await cards_collection.count_documents(
            {**deck_filter, "last_reviewed": {"$ne": None}}
        )
        due = await cards_collection.count_documents(
            {**deck_filter, "last_reviewed": {"$ne": None}, "next_review": {"$lte": now}}
        )

        # Calculate mastery from average ease factor
        mastery = 0
        if reviewed > 0:
            pipe = [
                {"$match": {**deck_filter, "last_reviewed": {"$ne": None}}},
                {"$group": {"_id": None, "avg_ease": {"$avg": "$ease_factor"}}},
            ]
            agg = await cards_collection.aggregate(pipe).to_list(length=1)
            if agg:
                avg_ease = agg[0].get("avg_ease", 2.5)
                mastery = round(((avg_ease - 1.3) / (2.5 - 1.3)) * 100)

        result.append({
            "deck_id": str(oid),
            "name": d.get("name", "Unnamed Deck"),
            "description": d.get("description", ""),
            "tags": d.get("tags", []),
            "total_cards": total,
            "reviewed_cards": reviewed,
            "due_cards": due,
            "mastery_pct": mastery,
        })

    return result


async def get_deck_content(user_id: str, deck_id: str) -> dict:
    """
    Returns a sample of cards from a specific deck (front/back text only).
    Capped at 20 cards to avoid token overload. Validates ownership.
    """
    try:
        deck_oid = ObjectId(deck_id)
    except Exception:
        return {"error": "Invalid deck ID"}

    deck = await decks_collection.find_one(
        {"_id": deck_oid, "user_id": user_id, "deleted_at": None}
    )
    if not deck:
        return {"error": "Deck not found or not accessible"}

    cards = await cards_collection.find(
        {"deck_id": {"$in": [deck_oid, deck_id]}, "deleted_at": None},
        {"title": 1, "content": 1, "tags": 1, "ease_factor": 1, "last_reviewed": 1}
    ).limit(20).to_list(length=20)

    card_summaries = []
    for c in cards:
        card_summaries.append({
            "front": c.get("title", ""),
            "back": c.get("content", "")[:300],  # Truncate long answers
            "tags": c.get("tags", []),
            "mastered": (c.get("ease_factor", 2.5) > 2.3),
            "reviewed": c.get("last_reviewed") is not None,
        })

    return {
        "deck_name": deck.get("name"),
        "description": deck.get("description", ""),
        "card_count": len(card_summaries),
        "cards": card_summaries,
    }


# ---------------------------------------------------------------------------
# Library Tools
# ---------------------------------------------------------------------------

async def list_books(user_id: str) -> list[dict]:
    """
    Returns all books in the user's library (title, author, summary).
    No content is fetched here — use read_book_section for that.
    """
    books = await books_collection.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 1, "title": 1, "author": 1, "summary": 1, "tags": 1, "updated_at": 1}
    ).to_list(length=100)

    return [
        {
            "book_id": str(b["_id"]),
            "title": b.get("title", "Untitled"),
            "author": b.get("author", "Unknown"),
            "summary": b.get("summary", "")[:200],
            "tags": b.get("tags", []),
            "last_updated": b.get("updated_at", "").strftime("%Y-%m-%d") if b.get("updated_at") else None,
        }
        for b in books
    ]


async def read_book_section(user_id: str, book_id: str, query: str = "") -> dict:
    """
    Fetches a section of a book's content as plain text. If a query is
    provided, it attempts to find the most relevant paragraph (simple
    keyword match). Always capped at 4,000 characters for token safety.
    Validates ownership before returning any content.
    """
    try:
        book_oid = ObjectId(book_id)
    except Exception:
        return {"error": "Invalid book ID"}

    book = await books_collection.find_one(
        {"_id": book_oid, "user_id": user_id, "deleted_at": None},
        {"title": 1, "author": 1, "full_content": 1}
    )
    if not book:
        return {"error": "Book not found or not accessible"}

    plaintext = _lexical_to_plaintext(book.get("full_content", ""), max_chars=8000)

    if not plaintext:
        return {"title": book.get("title"), "content": "No readable content found in this book."}

    # If a query is given, try to find a relevant paragraph
    excerpt = plaintext
    if query and len(plaintext) > 4000:
        query_terms = query.lower().split()
        paragraphs = [p.strip() for p in plaintext.split("\n") if p.strip()]
        scored = []
        for p in paragraphs:
            p_lower = p.lower()
            score = sum(1 for term in query_terms if term in p_lower)
            scored.append((score, p))
        # Sort by relevance and take the top paragraphs
        scored.sort(key=lambda x: x[0], reverse=True)
        excerpt = "\n\n".join(p for _, p in scored[:8])[:4000]
    else:
        excerpt = plaintext[:4000]

    return {
        "title": book.get("title"),
        "author": book.get("author"),
        "content": excerpt,
        "note": "Content is limited to 4,000 characters. Ask for a specific topic for more targeted results.",
    }


# ---------------------------------------------------------------------------
# Annual Plan & Goal Tools
# ---------------------------------------------------------------------------

async def get_annual_plan_context(user_id: str) -> dict:
    """
    Returns the user's current Annual Plan: active focus areas, their goals,
    and milestone completion status. Used to give the agent an understanding
    of the user's bigger picture learning objectives.
    """
    current_year = datetime.now(timezone.utc).year

    plan = await annual_plans_collection.find_one(
        {"user_id": user_id, "year": current_year, "deleted_at": None}
    )
    if not plan:
        return {"message": f"No annual plan found for {current_year}."}

    plan_id = str(plan["_id"])

    # Fetch focus areas
    areas = await focus_areas_collection.find(
        {"annual_plan_id": plan_id, "deleted_at": None},
        {"_id": 1, "name": 1, "description": 1}
    ).to_list(length=20)

    area_summaries = []
    for area in areas:
        area_id = str(area["_id"])

        # Fetch goals for this area
        goals = await goals_collection.find(
            {"focus_area_id": area_id, "deleted_at": None},
            {"_id": 1, "title": 1, "status": 1, "quarter": 1, "due_date": 1}
        ).to_list(length=20)

        area_summaries.append({
            "focus_area": area.get("name"),
            "description": area.get("description", ""),
            "goals": [
                {
                    "title": g.get("title"),
                    "status": g.get("status", "pending"),
                    "quarter": g.get("quarter"),
                }
                for g in goals
            ],
        })

    return {
        "year": current_year,
        "focus_areas": area_summaries,
        "total_focus_areas": len(area_summaries),
    }


async def get_user_interests(user_id: str) -> dict:
    """
    Fetches the user's onboarding preferences: interests, primary topic,
    and study goal. Useful for recommendation-style queries.
    """
    from app.config.database import users_collection

    user = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"preferences": 1, "full_name": 1}
    )
    if not user:
        return {"error": "User not found"}

    prefs = user.get("preferences", {}).get("general", {})
    return {
        "preferred_name": user.get("full_name", ""),
        "interests": prefs.get("interests", []),
        "primary_topic": prefs.get("primary_topic", ""),
        "study_goal": prefs.get("study_goal", "general"),
    }
