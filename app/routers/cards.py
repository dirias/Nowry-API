# app/routers/card.py

import asyncio
import json
import random
import time
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from pymongo.collection import Collection
from app.ai_orchestrator.llm_clients.gemini_client import (
    GeminiQuotaError,
    GeminiTransientError,
)
from app.core.model_config import get_client_for_tier, TIER_MODEL_NAMES
from app.core.langfuse_client import get_langfuse_client
from langfuse import propagate_attributes
from app.core import prompt_manager
from app.core import prompts as _prompts
from app.models.StudyCard import StudyCard
from app.models.CardGenerationRequest import (
    CardGenerationRequest,
    compute_effective_cap,
)
from app.models.book_generation import (
    GenerateFromBookRequest,
    GenerateFromBookResponse,
    GeneratedCard,
)
from app.models.card_stream import (
    SSE_HEARTBEAT,
    CardEventData,
    DoneEventData,
    ErrorEventData,
    sse_event,
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

MAX_BOOK_TEXT_CHARS: int = 50_000

# SSE streaming tuning (POST /card/generate/stream)
STREAM_HEARTBEAT_INTERVAL_S: float = 15.0
STREAM_TIMEOUT_S: float = 120.0
STREAM_CARD_PACING_S: float = 0.08


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


@router.post(
    "/generate",
    summary="Generate a new card using AI",
    response_model=list[GeneratedCard],
)
async def generate_card(
    payload: CardGenerationRequest,
    current_user: dict = Depends(track_ai_usage),
) -> list[GeneratedCard]:
    # CARD-01: Free users receive full card generation via Groq (Llama 3.3).
    # Tier is extracted from the user doc and forwarded to the orchestrator,
    # which routes to Groq for 'free' or Gemini for 'plus'/'pro'.
    # No caps or quotas are applied per D-01 (CONTEXT.md).
    # TODO: AI usage limit enforcement is pending (Phase 4 deferred — WR-01)
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    # None sampleNumber => adaptive mode: deterministic content-derived cap.
    effective_cap: int = compute_effective_cap(payload.sampleText, payload.sampleNumber)
    adaptive: bool = payload.sampleNumber is None
    logger.info(f"[cards] tier={tier} adaptive={adaptive} cap={effective_cap}")
    try:
        logger.info(f"Received generation request: {payload}")
        result = orchestrator.invoke(
            "rag",
            {
                "prompt": payload.prompt,
                "sampleText": payload.sampleText,
                "sampleNumber": effective_cap,
                "adaptive": adaptive,
                "excludeTitles": payload.excludeTitles,
                "tier": tier,
            },
        )
        logger.info("Card generation completed successfully.")
        cards: list[GeneratedCard] = [
            GeneratedCard.model_validate(card) for card in result["generated_cards"]
        ]
        if len(cards) > effective_cap:
            logger.warning(
                f"[cards] Model returned {len(cards)} cards — clipping to "
                f"cap {effective_cap}"
            )
            cards = cards[:effective_cap]
        return cards
    except HTTPException as http_err:
        logger.error(f"Generation failed with HTTP error: {http_err.detail}")
        raise http_err
    except Exception as ex:
        logger.exception(f"Unexpected error during card generation: {ex}")
        raise HTTPException(status_code=500, detail=str(ex))


@router.post(
    "/generate/stream",
    summary="Generate cards using AI, streamed as Server-Sent Events",
)
async def generate_card_stream(
    payload: CardGenerationRequest,
    current_user: dict = Depends(track_ai_usage),
) -> StreamingResponse:
    # NOTE: the RAG pipeline is atomic — one non-streaming LLM call in
    # text_node.py. Cards are emitted sequentially AFTER pipeline completion.
    # The SSE contract is the permanent transport; when the Gemini client
    # gains token streaming plus incremental JSON-array parsing, only steps
    # 2–5 of this generator change — the wire contract and the frontend do not.
    #
    # Auth: the router-level Depends(get_firebase_user) plus track_ai_usage
    # resolve BEFORE this handler returns a StreamingResponse, so auth
    # failures surface as plain HTTP 401/403 with no SSE bytes.
    tier: str = current_user.get("subscription", {}).get("tier", "free")
    # None sampleNumber => adaptive mode: deterministic content-derived cap,
    # computed here BEFORE the orchestrator is invoked.
    effective_cap: int = compute_effective_cap(payload.sampleText, payload.sampleNumber)
    adaptive: bool = payload.sampleNumber is None
    logger.info(
        f"[generate_card_stream] tier={tier} adaptive={adaptive} cap={effective_cap}"
    )

    async def event_generator() -> AsyncGenerator[str, None]:
        start: float = time.monotonic()
        task: "asyncio.Task[dict]" = asyncio.create_task(
            asyncio.to_thread(
                orchestrator.invoke,
                "rag",
                {
                    "prompt": payload.prompt,
                    "sampleText": payload.sampleText,
                    "sampleNumber": effective_cap,
                    "adaptive": adaptive,
                    "excludeTitles": payload.excludeTitles,
                    "tier": tier,
                },
            )
        )
        try:
            # Heartbeat loop — keeps the connection alive while the blocking
            # pipeline runs off the event loop in a worker thread.
            while not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=STREAM_HEARTBEAT_INTERVAL_S
                )
                if done:
                    break
                if time.monotonic() - start > STREAM_TIMEOUT_S:
                    # The pipeline thread cannot be cancelled — known,
                    # accepted limitation: let it finish detached.
                    logger.warning(
                        "[generate_card_stream] Generation exceeded "
                        f"{STREAM_TIMEOUT_S:.0f}s — abandoning pipeline "
                        "thread (it will finish detached)."
                    )
                    yield sse_event(
                        "error",
                        ErrorEventData(
                            code="STREAM_TIMEOUT",
                            message="Generation exceeded 120s",
                        ),
                    )
                    return
                yield SSE_HEARTBEAT

            try:
                result: dict = task.result()
            except GeminiQuotaError:
                logger.exception("[generate_card_stream] AI quota exhausted")
                yield sse_event(
                    "error",
                    ErrorEventData(
                        code="AI_QUOTA_EXHAUSTED",
                        message="AI quota exhausted. Please try again later.",
                    ),
                )
                return
            except Exception:
                logger.exception("[generate_card_stream] AI pipeline failed")
                yield sse_event(
                    "error",
                    ErrorEventData(
                        code="AI_PIPELINE_FAILED",
                        message="AI pipeline failed. Please try again.",
                    ),
                )
                return

            if result.get("parse_error"):
                logger.error(
                    "[generate_card_stream] LLM returned malformed output"
                )
                yield sse_event(
                    "error",
                    ErrorEventData(
                        code="AI_MALFORMED_OUTPUT",
                        message="AI returned unexpected format. Please try again.",
                    ),
                )
                return

            raw_cards: list = result.get("generated_cards") or []
            valid_cards: list[GeneratedCard] = []
            for raw_index, raw_card in enumerate(raw_cards):
                try:
                    valid_cards.append(GeneratedCard.model_validate(raw_card))
                except ValidationError as exc:
                    logger.warning(
                        f"[generate_card_stream] Dropping invalid card at "
                        f"index {raw_index}: {exc}"
                    )

            truncated: bool = len(valid_cards) > effective_cap
            if truncated:
                logger.warning(
                    f"[generate_card_stream] Model returned {len(valid_cards)} "
                    f"cards — clipping to cap {effective_cap}"
                )
                valid_cards = valid_cards[:effective_cap]

            total: int = len(valid_cards)
            for index, card in enumerate(valid_cards):
                yield sse_event(
                    "card", CardEventData(index=index, total=total, card=card)
                )
                if index < total - 1:
                    # Progressive-render pacing for the frontend.
                    await asyncio.sleep(STREAM_CARD_PACING_S)

            elapsed_ms: int = int((time.monotonic() - start) * 1000)
            yield sse_event(
                "done",
                DoneEventData(
                    total_cards=total,
                    elapsed_ms=elapsed_ms,
                    mode="auto" if adaptive else "fixed",
                    cap=effective_cap,
                    truncated=truncated,
                ),
            )
            return
        except asyncio.CancelledError:
            logger.info(
                "[generate_card_stream] Client disconnected — stream cancelled"
            )
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    llm_client = get_client_for_tier(tier)
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    system_prompt = prompt_manager.get_prompt(
        "nowry-book-cards",
        card_limit=card_limit if card_limit else "as many as appropriate",
    )
    system_prompt = f"{system_prompt}\n\n{_prompts.MATH_NOTATION_INSTRUCTION}"
    user_prompt = f"Book content:\n{plain_text}"

    client = get_langfuse_client()
    model_name = TIER_MODEL_NAMES.get(tier, TIER_MODEL_NAMES["free"])
    trace_metadata = {"feature": "book_cards", "tier": tier, "user_id": user_id, "model": model_name}

    # Retry only on transient errors; fail fast on quota exhaustion.
    # Backoff: attempt 1 → immediate, attempt 2 → ~1 s, attempt 3 → ~2 s (with jitter).
    _MAX_ATTEMPTS: int = 3
    _BACKOFF_BASE: float = 1.0  # seconds

    raw_text: str = ""
    last_exc: Exception | None = None
    combined_prompt: str = f"{system_prompt}\n\n{user_prompt}"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay: float = _BACKOFF_BASE * (2 ** (attempt - 2)) + random.uniform(0.0, 0.3)
            await asyncio.sleep(delay)
        try:
            if client:
                try:
                    with propagate_attributes(
                        user_id=user_id,
                        trace_name="book_cards",
                        metadata=trace_metadata,
                        tags=["book_cards", tier],
                    ):
                        with client.start_as_current_observation(
                            name="book_cards",
                            as_type="generation",
                            model=model_name,
                            input=[{"role": "user", "content": combined_prompt}],
                            model_parameters={"card_limit": card_limit},
                        ) as generation:
                            completion = llm_client.request(combined_prompt)
                            raw_text = (completion.choices[0].message.content or "").strip()
                            # D-13: full output, no truncation. Gemini wrapper exposes no usage -> None.
                            generation.update(output=raw_text, usage_details=None)
                except (GeminiQuotaError, GeminiTransientError, HTTPException):
                    raise
                except Exception as langfuse_exc:
                    logger_cards.warning(
                        f"[generate_cards_from_book] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                    )
                    completion = llm_client.request(combined_prompt)
                    raw_text = (completion.choices[0].message.content or "").strip()
            else:
                completion = llm_client.request(combined_prompt)
                raw_text = (completion.choices[0].message.content or "").strip()

            if raw_text:
                break
            logger_cards.warning(f"[generate_cards_from_book] Empty response attempt {attempt}")
        except HTTPException:
            raise
        except GeminiQuotaError as exc:
            logger_cards.warning(f"[generate_cards_from_book] Quota exhausted attempt {attempt}: {exc}")
            raise HTTPException(status_code=503, detail="AI service error. Please try again.")
        except (GeminiTransientError, Exception) as exc:
            last_exc = exc
            logger_cards.warning(f"[generate_cards_from_book] LLM error attempt {attempt}: {exc}")

    if not raw_text:
        raise HTTPException(status_code=503, detail="AI service error. Please try again.")

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

    llm_client = get_client_for_tier(tier)  # always Gemini Pro for Pro tier
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    system_prompt = (
        "You are a flashcard quality analyst. Analyze the provided deck cards and return a JSON object with:\n"
        "- 'duplicates': list of {card_a_id, card_b_id, reason} for semantically similar cards\n"
        "- 'gaps': list of {topic, description} for important topics not covered\n"
        "- 'rewrite_suggestions': list of {card_id, original_front, original_back, suggested_front, suggested_back, reason}\n"
        "Return ONLY valid JSON with no markdown fences. Use the bracketed card IDs in your response."
    )
    user_prompt = f"Deck name: {deck.get('name', 'Unknown')}\n\nCards:\n{cards_text}"

    # Retry only on transient errors; fail fast on quota exhaustion.
    # Backoff: attempt 1 → immediate, attempt 2 → ~1 s, attempt 3 → ~2 s (with jitter).
    _MAX_ATTEMPTS: int = 3
    _BACKOFF_BASE: float = 1.0  # seconds

    raw_text: str = ""
    last_exc: Exception | None = None
    combined_prompt: str = f"{system_prompt}\n\n{user_prompt}"

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay: float = _BACKOFF_BASE * (2 ** (attempt - 2)) + random.uniform(0.0, 0.3)
            await asyncio.sleep(delay)
        try:
            completion = llm_client.request(combined_prompt)
            raw_text = (completion.choices[0].message.content or "").strip()
            if raw_text:
                break
            logger_cards.warning(f"[analyze_deck] Empty response attempt {attempt}")
        except GeminiQuotaError as exc:
            logger_cards.warning(f"[analyze_deck] Quota exhausted attempt {attempt}: {exc}")
            raise HTTPException(status_code=503, detail="AI service error. Please try again.")
        except (GeminiTransientError, Exception) as exc:
            last_exc = exc
            logger_cards.warning(f"[analyze_deck] LLM error attempt {attempt}: {exc}")

    if not raw_text:
        raise HTTPException(status_code=503, detail="AI service error. Please try again.")

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
