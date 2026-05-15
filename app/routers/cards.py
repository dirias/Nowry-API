# app/routers/card.py

import json
import os
from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from groq import Groq
from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client
from app.models.StudyCard import StudyCard
from app.models.CardGenerationRequest import CardGenerationRequest
from app.models.book_generation import (
    GenerateFromBookRequest,
    GenerateFromBookResponse,
    GeneratedCard,
)
from app.models.deck_analysis import (
    DeckAnalysisRequest,
    DeckAnalysisResponse,
    DuplicatePair,
    TopicGap,
    RewriteSuggestion,
)
from app.config.database import cards_collection, books_collection, decks_collection
from app.ai_orchestrator.orchestrator import orchestrator
from app.auth.firebase_auth import get_firebase_user
from app.auth.dependencies import track_ai_usage, get_subscription_tier
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/card",
    tags=["cards"],
    dependencies=[Depends(get_firebase_user)],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)
logger_cards = get_logger(__name__)

_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_BOOK_TEXT_CHARS: int = 50_000


def _get_groq_client_cards() -> Groq:
    api_key: str = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY environment variable is not set")
    return Groq(api_key=api_key)


def _get_llm_client_for_tier_cards(tier: str):
    if tier == "free":
        return _get_groq_client_cards()
    elif tier == "plus":
        return Gemini_client("models/gemini-flash-latest")
    elif tier == "pro":
        return Gemini_client("models/gemini-pro-latest")
    else:
        raise ValueError(f"Unknown subscription tier: {tier!r}. Expected 'free', 'plus', or 'pro'.")


def _extract_text_from_lexical(lexical_state: dict) -> str:
    """Recursively walk Lexical JSON state, collecting text node values."""
    texts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                texts.append(node["text"])
            for key in ("children", "root"):
                if key in node:
                    walk(node[key])

    walk(lexical_state)
    return " ".join(texts)


def get_cards_collection() -> Collection:
    return cards_collection


@router.post("/generate", summary="Generate a new card using AI")
async def generate_card(
    payload: CardGenerationRequest,
    current_user: dict = Depends(track_ai_usage),
) -> dict:
    # CARD-01: Free users receive full card generation via Groq (Llama 3.3).
    # Tier is extracted from the user doc and forwarded to the orchestrator,
    # which routes to Groq for 'free' or Gemini for 'plus'/'pro'.
    # No caps or quotas are applied per D-01 (CONTEXT.md).
    # TODO: AI usage limit enforcement is pending (Phase 4 deferred — WR-01)
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    logger.info(f"[cards] tier={tier}")
    try:
        logger.info(f"Received generation request: {payload}")
        result = orchestrator.invoke(
            "rag",
            {
                "prompt": payload.prompt,
                "sampleText": payload.sampleText,
                "sampleNumber": payload.sampleNumber,
                "tier": tier,
            },
        )
        logger.info("Card generation completed successfully.")
        return result["generated_cards"]
    except HTTPException as http_err:
        logger.error(f"Generation failed with HTTP error: {http_err.detail}")
        raise http_err
    except Exception as ex:
        logger.exception(f"Unexpected error during card generation: {ex}")
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/create", summary="Create a new card", response_model=StudyCard)
async def create_card(
    card: StudyCard,
    cards_collection: Collection = Depends(get_cards_collection),
    current_user: dict = Depends(get_firebase_user)
):
    logger.info(f"Creating card: {card.title}")
    
    card_dict = card.model_dump()
    # Security: Force user_id to be the authenticated user to prevent IDOR
    card_dict["user_id"] = current_user.get("user_id")
    
    result = await cards_collection.insert_one(card_dict)
    logger.info(f"Card created with ID: {result.inserted_id}")
    return {**card_dict, "id": str(result.inserted_id)}


@router.post("/generate-from-book", response_model=GenerateFromBookResponse)
async def generate_cards_from_book(
    body: GenerateFromBookRequest,
    current_user: dict = Depends(track_ai_usage),
    tier: str = Depends(get_subscription_tier),
) -> GenerateFromBookResponse:
    """Generate flashcards from full book content. Plus+ only."""
    if tier == "free":
        raise HTTPException(status_code=403, detail="Book-wide card generation requires Plus or Pro.")

    user_id: str = current_user.get("user_id", "")

    try:
        from bson import ObjectId as _ObjId
        book = await books_collection.find_one({"_id": _ObjId(body.book_id), "deleted_at": None})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")

    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    raw_content: str = book.get("full_content", "")
    try:
        lexical_state = json.loads(raw_content)
        plain_text = _extract_text_from_lexical(lexical_state)
    except (json.JSONDecodeError, KeyError):
        plain_text = raw_content

    if len(plain_text) > MAX_BOOK_TEXT_CHARS:
        logger_cards.warning(
            f"[generate_cards_from_book] Book text truncated {len(plain_text)} → {MAX_BOOK_TEXT_CHARS}"
        )
        plain_text = plain_text[:MAX_BOOK_TEXT_CHARS]

    if not plain_text.strip():
        raise HTTPException(status_code=400, detail="Book has no text content to analyze.")

    card_limit = 20 if tier == "plus" else None  # Pro = unlimited

    llm_client = _get_llm_client_for_tier_cards(tier)

    system_prompt = (
        "You are a flashcard generation expert. Given book content, generate high-quality "
        "study flashcards. Return ONLY a JSON array with no markdown fences. "
        "Each card must have 'title' (front of card, a question or concept) and "
        "'content' (back of card, the answer or explanation). "
        f"Generate at most {card_limit if card_limit else 'as many as appropriate'} cards."
    )
    user_prompt = f"Book content:\n{plain_text}"

    raw_text: str = ""
    last_exc: Exception | None = None
    for attempt in range(1, 3):
        try:
            combined_prompt = f"{system_prompt}\n\n{user_prompt}"
            completion = llm_client.request(combined_prompt)
            raw_text = (completion.choices[0].message.content or "").strip()
            if raw_text:
                break
            logger_cards.warning(f"[generate_cards_from_book] Empty response attempt {attempt}")
        except HTTPException:
            raise
        except Exception as exc:
            last_exc = exc
            logger_cards.warning(f"[generate_cards_from_book] LLM error attempt {attempt}: {exc}")

    if not raw_text:
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

    # Strip markdown fences
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed: list[dict] = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger_cards.error(f"[generate_cards_from_book] Malformed JSON: {exc}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned unexpected format. Please try again.")

    if card_limit:
        parsed = parsed[:card_limit]

    cards = [GeneratedCard(title=c.get("title", ""), content=c.get("content", "")) for c in parsed]
    return GenerateFromBookResponse(cards=cards)


@router.post("/analyze-deck", response_model=DeckAnalysisResponse)
async def analyze_deck(
    body: DeckAnalysisRequest,
    current_user: dict = Depends(track_ai_usage),
    tier: str = Depends(get_subscription_tier),
) -> DeckAnalysisResponse:
    """Pro-only: analyze a full deck for duplicates, topic gaps, and rewrite suggestions."""
    if tier != "pro":
        raise HTTPException(status_code=403, detail="Deck analysis requires Pro.")

    user_id: str = current_user.get("user_id", "")

    try:
        from bson import ObjectId as _ObjId
        deck_oid = _ObjId(body.deck_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deck ID.")

    # Verify deck ownership
    deck = await decks_collection.find_one({"_id": deck_oid, "user_id": user_id, "deleted_at": None})
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found.")

    # Load all cards in batches of 25 — use $or for ObjectId/string deck_id mismatch
    BATCH_SIZE = 25
    skip = 0
    all_cards: list[dict] = []
    deck_or = [{"deck_id": deck_oid}, {"deck_id": str(deck_oid)}]
    base_query = {"user_id": user_id, "deleted_at": None, "$or": deck_or}

    while True:
        batch = await cards_collection.find(base_query).skip(skip).to_list(length=BATCH_SIZE)
        if not batch:
            break
        all_cards.extend(batch)
        skip += BATCH_SIZE
        if len(batch) < BATCH_SIZE:
            break

    if not all_cards:
        return DeckAnalysisResponse(duplicates=[], gaps=[], rewrite_suggestions=[])

    # Serialize cards for LLM (title + content only, with string _id)
    cards_text = "\n".join(
        f"[{str(c['_id'])}] Front: {c.get('title', '')} | Back: {c.get('content', '')}"
        for c in all_cards
    )

    llm_client = _get_llm_client_for_tier_cards(tier)  # always Gemini Pro for Pro tier

    system_prompt = (
        "You are a flashcard quality analyst. Analyze the provided deck cards and return a JSON object with:\n"
        "- 'duplicates': list of {card_a_id, card_b_id, reason} for semantically similar cards\n"
        "- 'gaps': list of {topic, description} for important topics not covered\n"
        "- 'rewrite_suggestions': list of {card_id, original_front, original_back, suggested_front, suggested_back, reason}\n"
        "Return ONLY valid JSON with no markdown fences. Use the bracketed card IDs in your response."
    )
    user_prompt = f"Deck name: {deck.get('name', 'Unknown')}\n\nCards:\n{cards_text}"

    raw_text: str = ""
    last_exc: Exception | None = None
    for attempt in range(1, 3):
        try:
            combined_prompt = f"{system_prompt}\n\n{user_prompt}"
            completion = llm_client.request(combined_prompt)
            raw_text = (completion.choices[0].message.content or "").strip()
            if raw_text:
                break
            logger_cards.warning(f"[analyze_deck] Empty response attempt {attempt}")
        except Exception as exc:
            last_exc = exc
            logger_cards.warning(f"[analyze_deck] LLM error attempt {attempt}: {exc}")

    if not raw_text:
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger_cards.error(f"[analyze_deck] Malformed JSON: {exc}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned unexpected format. Please try again.")

    duplicates = [DuplicatePair(**d) for d in parsed.get("duplicates", [])]
    gaps = [TopicGap(**g) for g in parsed.get("gaps", [])]
    rewrites = [RewriteSuggestion(**r) for r in parsed.get("rewrite_suggestions", [])]

    return DeckAnalysisResponse(duplicates=duplicates, gaps=gaps, rewrite_suggestions=rewrites)
