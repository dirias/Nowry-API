from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel
from bson import ObjectId
import zipfile
import sqlite3
import tempfile
import os
import re

from app.auth.firebase_auth import get_firebase_user
from app.config.database import decks_collection, cards_collection, users_collection
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/import",
    tags=["import"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)

MAX_FILE_SIZE_MB = 50
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
PREVIEW_CARD_LIMIT = 5


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class ParsedCard(BaseModel):
    front: str
    back: str
    tags: List[str] = []


class ParsedDeckPreview(BaseModel):
    suggested_name: str
    card_count: int
    cards_allowed: int  # remaining quota (-1 = unlimited)
    preview_cards: List[ParsedCard]
    all_cards: List[ParsedCard]


class ImportConfirmPayload(BaseModel):
    deck_name: str
    description: Optional[str] = None
    cards: List[ParsedCard]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return text.strip()


def _parse_apkg(file_bytes: bytes, filename: str) -> tuple[str, List[ParsedCard]]:
    """
    Parse an .apkg file, returning (suggested_deck_name, list_of_cards).
    Raises ValueError on any format error.
    """
    with tempfile.TemporaryDirectory() as tmp:
        apkg_path = os.path.join(tmp, "deck.apkg")
        with open(apkg_path, "wb") as f:
            f.write(file_bytes)

        # Validate zip structure
        if not zipfile.is_zipfile(apkg_path):
            raise ValueError("File is not a valid .apkg (not a zip archive).")

        with zipfile.ZipFile(apkg_path, "r") as z:
            names = z.namelist()

            # Pick database — prefer anki21
            db_name = None
            for candidate in ["collection.anki21", "collection.anki2"]:
                if candidate in names:
                    db_name = candidate
                    break

            if not db_name:
                raise ValueError("No Anki collection database found inside the .apkg file.")

            # --- Zip Bomb Mitigation ---
            info = z.getinfo(db_name)
            # Limit the uncompressed SQLite DB to 150MB
            if info.file_size > 150 * 1024 * 1024:
                raise ValueError("Database file is dangerously large. Import aborted.")
            # ---------------------------

            z.extract(db_name, tmp)

        db_path = os.path.join(tmp, db_name)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # -- Deck name from col table -----------------------------------------
        suggested_name = "Imported Deck"
        try:
            import json as _json
            cur.execute("SELECT decks FROM col LIMIT 1")
            row = cur.fetchone()
            if row:
                decks_json = _json.loads(row[0])
                # Anki stores decks as {id: {name: ...}}. Skip the default deck (id=1).
                non_default = [
                    v["name"]
                    for k, v in decks_json.items()
                    if k != "1" and isinstance(v, dict) and "name" in v
                ]
                if non_default:
                    # Use top-level name (strip "::" hierarchy if present)
                    suggested_name = non_default[0].split("::")[-1].strip()
        except Exception:
            pass  # Fall back to default name

        # -- Cards from notes table -------------------------------------------
        try:
            cur.execute("SELECT flds, tags FROM notes")
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            raise ValueError("Could not read cards from the Anki database. The file may be corrupt.")

        conn.close()

        # Known compatibility shims Anki embeds when the format is too new
        COMPAT_MESSAGES = {
            "Please update to the latest Anki version, then import the .colpkg/.apkg file again.",
            "Please update to the latest Anki version.",
        }

        cards: List[ParsedCard] = []
        compat_notes_found = 0
        for flds, tags in rows:
            # Anki fields are \x1f separated
            raw_fields = flds.split("\x1f")
            
            # Clean all fields
            cleaned_fields = [_strip_html(f) for f in raw_fields]
            
            # Front is usually the first field (Expression/Kanji)
            front = cleaned_fields[0] if len(cleaned_fields) > 0 else ""
            
            # Back: Join all other non-empty fields (Reading + Meaning + Examples)
            other_fields = [f for f in cleaned_fields[1:] if f.strip()]
            back = "\n\n".join(other_fields) if other_fields else ""

            # Skip completely empty cards
            if not front and not back:
                continue

            # Skip Anki compatibility-shim notes (newer format warning)
            if front.strip() in COMPAT_MESSAGES:
                compat_notes_found += 1
                continue

            # Clean and split tags (Anki stores them inside spaces " tag1 tag2 ")
            tag_list = [t.strip() for t in (tags or "").strip().split() if t.strip()]
            cards.append(ParsedCard(front=front, back=back, tags=tag_list))

        if not cards:
            if compat_notes_found > 0:
                raise ValueError(
                    "This .apkg was exported with a newer version of Anki (23.10+) that uses a "
                    "format our parser does not yet support. To import it, please re-export "
                    "from Anki using File → Export → 'Anki Deck Package (.apkg)' with the "
                    "'Support older Anki versions' option enabled."
                )
            raise ValueError("No cards found in the .apkg file.")

        return suggested_name, cards


async def _get_remaining_quota(user_id: str) -> int:
    """Return remaining flashcard quota. -1 means unlimited."""
    try:
        from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier
        user_data = await users_collection.find_one({"_id": ObjectId(user_id)})
        if not user_data:
            return 0

        subscription_data = user_data.get("subscription", {"tier": "free"})
        tier_key = subscription_data.get("tier", "free")
        plan = SUBSCRIPTION_PLANS.get(tier_key, SUBSCRIPTION_PLANS[SubscriptionTier.FREE])
        limit = plan["limits"].get("flashcards", 0)

        if limit == -1:
            return -1

        current_count = await cards_collection.count_documents({"user_id": user_id})
        return max(0, limit - current_count)
    except Exception:
        return -1  # Default to unlimited on error


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "/apkg",
    summary="Parse an Anki .apkg file and return a preview",
    response_model=ParsedDeckPreview,
)
async def parse_apkg(
    file: UploadFile = File(...),
    user: dict = Depends(get_firebase_user),
):
    """
    Step 1: Upload and parse an .apkg file.
    Returns a preview with the first few cards and metadata.
    No database writes happen at this step.
    """
    user_id = user.get("user_id")
    logger.info(f"User {user_id} uploading .apkg: {file.filename}")

    # Validate extension
    if not (file.filename or "").lower().endswith(".apkg"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .apkg files are supported.",
        )

    # Read and validate size
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {MAX_FILE_SIZE_MB} MB limit.",
        )

    # Parse
    try:
        suggested_name, all_cards = _parse_apkg(file_bytes, file.filename or "deck.apkg")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected parse error: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to parse the .apkg file.")

    cards_allowed = await _get_remaining_quota(user_id)

    return ParsedDeckPreview(
        suggested_name=suggested_name,
        card_count=len(all_cards),
        cards_allowed=cards_allowed,
        preview_cards=all_cards[:PREVIEW_CARD_LIMIT],
        all_cards=all_cards,
    )


@router.post(
    "/apkg/confirm",
    summary="Confirm and write imported Anki deck + cards to the database",
    status_code=status.HTTP_201_CREATED,
)
async def confirm_import(
    payload: ImportConfirmPayload,
    user: dict = Depends(get_firebase_user),
):
    """
    Step 2: Confirm the import after the user has reviewed the preview.
    Creates the deck and bulk-inserts all cards with SM-2 defaults.
    """
    user_id = user.get("user_id")
    logger.info(f"User {user_id} confirming import of deck '{payload.deck_name}' with {len(payload.cards)} cards")

    if not payload.cards:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No cards to import.")

    # Quota check (single check against total import count)
    cards_allowed = await _get_remaining_quota(user_id)
    if cards_allowed != -1 and len(payload.cards) > cards_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Importing {len(payload.cards)} cards would exceed your plan limit. You have {cards_allowed} slots remaining.",
        )

    now = datetime.utcnow()

    # 1. Create the deck
    deck_doc = {
        "user_id": user_id,
        "name": payload.deck_name.strip(),
        "description": payload.description or "",
        "deck_type": "flashcard",
        "total_cards": len(payload.cards),
        "status": "new",
        "tags": [],
        "cards": [],
        "is_public": False,
        "voice_settings": {
            "front": {"voice_name": None, "rate": 1.0, "pitch": 1.0},
            "back": {"voice_name": None, "rate": 1.0, "pitch": 1.0},
        },
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }

    deck_result = await decks_collection.insert_one(deck_doc)
    deck_id = deck_result.inserted_id

    # 2. Bulk-insert cards with SM-2 defaults
    card_docs = [
        {
            "user_id": user_id,
            "deck_id": deck_id,
            "title": card.front,
            "content": card.back,
            "card_type": "flashcard",
            "tags": card.tags,
            "ease_factor": 2.5,
            "interval": 1,
            "repetitions": 0,
            "last_reviewed": None,
            "next_review": None,
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        for card in payload.cards
    ]

    cards_result = await cards_collection.insert_many(card_docs)
    card_ids = cards_result.inserted_ids

    # 3. Update deck with card references and correct total count
    await decks_collection.update_one(
        {"_id": deck_id},
        {"$set": {"cards": card_ids, "total_cards": len(card_ids)}},
    )

    logger.info(f"Successfully imported deck '{payload.deck_name}' with {len(card_ids)} cards for user {user_id}")

    return {
        "deck_id": str(deck_id),
        "deck_name": payload.deck_name,
        "cards_imported": len(card_ids),
    }
