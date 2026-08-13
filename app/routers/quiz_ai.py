"""
AI Quiz Router — topic-based quiz sessions with Claude-generated questions.

Endpoints:
  POST /v1/assistant/quiz/start-ai  — Generate N questions on any topic, cache
                                       server-side, return the first question.

The subsequent questions are served via the existing POST /v1/assistant/quiz/answer
endpoint (see routers/quiz.py), which detects the session_type field in the session
document and routes into the AI answer handler.

Design decisions:
  - All N questions are generated upfront in a single Claude API call to minimise
    latency during the session and avoid repeated LLM round-trips per question.
  - Questions are stored in the ai_quiz_sessions collection with a 24-hour TTL.
  - This endpoint does NOT decrement the user's message budget.
  - Free-tier users are limited to 10 questions regardless of the request body;
    Plus/Pro users may request 1–20 questions (configured in agent preferences).
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request

from langfuse import propagate_attributes

from app.auth.dependencies import get_subscription_tier, track_ai_usage
from app.core.langfuse_client import get_langfuse_client
from app.core.model_config import get_client_for_tier, TIER_MODEL_NAMES
from app.core import prompt_manager
from app.core import prompts as _prompts
from app.config.database import (
    ai_quiz_sessions_collection,
    books_collection,
    cards_collection,
    decks_collection,
    users_collection,
)
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier
from app.core.limiter import limiter
from app.models.book_generation import (
    GeneratedQuizQuestion,
    GenerateQuizFromBookRequest,
    GenerateQuizFromBookResponse,
)
from app.models.deck_quiz_analysis import DeckQuizAnalysisResponse
from app.models.quiz import (
    AIQuizQuestionResponse,
    AIQuizQuestionStored,
    AIQuizStartRequest,
    AIQuizStartResponse,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["quiz"])

# ---------------------------------------------------------------------------
# Language name map for system prompt localisation
# ---------------------------------------------------------------------------

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "pt": "Portuguese",
    "it": "Italian",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_tier(user: dict) -> SubscriptionTier:
    """Extract the SubscriptionTier from a user document safely."""
    raw_tier: str = user.get("subscription", {}).get("tier", "free")
    try:
        return SubscriptionTier(raw_tier)
    except ValueError:
        return SubscriptionTier.FREE


def _resolve_question_count(
    requested: int,
    user: dict,
    tier: str,
) -> int:
    """
    Return the effective question count for this session.

    Free tier is always capped at 10.
    Plus/Pro respect the user-configured preference (ai_quiz_question_count),
    clamped to the requested value and the hard maximum of 20.
    """
    if tier == "free":
        return 5  # per QUIZ-01 / D-03

    configured: int = (
        user.get("preferences", {})
        .get("agent", {})
        .get("ai_quiz_question_count", 10)
    )
    # The user-configured value is the session maximum; the caller may request fewer.
    effective = min(requested, configured, 20)
    return max(effective, 1)


_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


async def _generate_questions(
    topic: str,
    question_count: int,
    language: str,
    tier: str,
    user_id: str,
    feature: str,  # D-09: "quiz_from_deck" (only current caller: start_ai_quiz_session)
) -> list[AIQuizQuestionStored]:
    """
    Call the tier-appropriate LLM to generate `question_count` questions on `topic`.

    The model is asked to return a JSON array of question objects. Each object
    must have the fields defined in AIQuizQuestionStored. We request three
    question types in a balanced mix to ensure variety.

    Returns a list of AIQuizQuestionStored instances.
    Raises HTTPException(502) on LLM API failure or malformed JSON.
    """
    llm_client = get_client_for_tier(tier)
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    client = get_langfuse_client()
    model_name = TIER_MODEL_NAMES.get(tier, TIER_MODEL_NAMES["free"])
    trace_metadata = {"feature": feature, "tier": tier, "user_id": user_id, "model": model_name}

    lang_name: str = _LANGUAGE_NAMES.get(language.split("-")[0].lower(), "English")

    system_prompt = prompt_manager.get_prompt("nowry-quiz-intent", lang_name=lang_name)

    variety_hint: str = random.choice([
        "Focus on mid-frequency items — skip the most beginner-obvious examples.",
        "Choose an eclectic, uncommon mix — pretend the learner already knows the basics.",
        "Prioritise variety: pick items scattered across the topic, not clustered at the easy end.",
        "Avoid the top-10 most common items for this topic. Go deeper.",
        "Imagine the learner has seen this topic before — challenge them with less obvious entries.",
    ])

    user_prompt: str = (
        f"Topic: {topic}\n"
        f"Number of questions: {question_count}\n"
        f"Selection hint: {variety_hint}\n\n"
        "Generate the JSON array of questions now."
    )

    raw_text: str = ""
    last_exc: Exception | None = None
    for attempt in range(1, 3):  # up to 2 attempts
        attempt_output: str = ""
        attempt_error: str | None = None
        try:
            if client:
                try:
                    with propagate_attributes(
                        user_id=user_id,
                        trace_name=feature,
                        metadata=trace_metadata,
                        tags=[feature, tier],
                    ):
                        with client.start_as_current_observation(
                            name=feature,
                            as_type="generation",
                            model=model_name,
                            input=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            model_parameters={"temperature": 0.7, "max_tokens": 4096},
                        ) as generation:
                            if tier == "free":
                                completion = llm_client.chat.completions.create(
                                    model=_GROQ_MODEL, max_tokens=4096, temperature=0.7,
                                    messages=[
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": user_prompt},
                                    ],
                                )
                                choice = completion.choices[0]
                                finish_reason: str = choice.finish_reason or "unknown"
                                raw_text = (choice.message.content or "").strip()
                                usage = getattr(completion, "usage", None)
                                usage_details = (
                                    {
                                        "input": getattr(usage, "prompt_tokens", 0),
                                        "output": getattr(usage, "completion_tokens", 0),
                                        "total": getattr(usage, "total_tokens", 0),
                                    }
                                    if usage
                                    else None
                                )
                            else:
                                combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                                completion = llm_client.request(combined_prompt)
                                choice = completion.choices[0]
                                finish_reason = "stop"
                                raw_text = (choice.message.content or "").strip()
                                usage_details = None

                            attempt_output = raw_text
                            # D-13: full output per attempt, no truncation
                            generation.update(output=attempt_output, usage_details=usage_details)
                except Exception as langfuse_exc:
                    logger.warning(
                        f"[ai_quiz] Langfuse tracing failed on attempt {attempt}, continuing without trace: {langfuse_exc}"
                    )
                    if tier == "free":
                        completion = llm_client.chat.completions.create(
                            model=_GROQ_MODEL, max_tokens=4096, temperature=0.7,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                        )
                        choice = completion.choices[0]
                        finish_reason = choice.finish_reason or "unknown"
                        raw_text = (choice.message.content or "").strip()
                    else:
                        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                        completion = llm_client.request(combined_prompt)
                        choice = completion.choices[0]
                        finish_reason = "stop"
                        raw_text = (choice.message.content or "").strip()
            else:
                if tier == "free":
                    completion = llm_client.chat.completions.create(
                        model=_GROQ_MODEL, max_tokens=4096, temperature=0.7,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    )
                    choice = completion.choices[0]
                    finish_reason = choice.finish_reason or "unknown"
                    raw_text = (choice.message.content or "").strip()
                else:
                    combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                    completion = llm_client.request(combined_prompt)
                    choice = completion.choices[0]
                    finish_reason = "stop"
                    raw_text = (choice.message.content or "").strip()

            if raw_text:
                break
            logger.warning(
                f"[ai_quiz] LLM returned empty content on attempt {attempt} "
                f"(finish_reason={finish_reason}, tier={tier}). Retrying…"
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(f"[ai_quiz] LLM API error on attempt {attempt} (tier={tier}): {exc}")

    if not raw_text:
        if last_exc:
            logger.error(f"[ai_quiz] LLM API failed after retries (tier={tier}): {last_exc}")
        else:
            logger.error(f"[ai_quiz] LLM returned empty content after retries (tier={tier})")
        raise HTTPException(
            status_code=502,
            detail="AI service error while generating quiz questions. Please try again.",
        )

    # Strip accidental markdown fences
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        parsed: list[dict] = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise ValueError("Expected a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(f"[ai_quiz] LLM returned malformed JSON: {exc}\nRaw: {raw_text[:500]}")
        raise HTTPException(
            status_code=502,
            detail="AI returned an unexpected format. Please try again.",
        )

    questions: list[AIQuizQuestionStored] = []
    for idx, item in enumerate(parsed[:question_count]):
        q_type = item.get("question_type", "short_answer")
        if q_type not in ("fill_in_blank", "multiple_choice", "short_answer"):
            q_type = "short_answer"

        options: list[str] | None = None
        if q_type == "multiple_choice":
            raw_options = item.get("options")
            if isinstance(raw_options, list) and len(raw_options) >= 2:
                options = [str(o) for o in raw_options[:4]]

        questions.append(
            AIQuizQuestionStored(
                card_id=str(uuid4()),
                question_type=q_type,
                question_text=str(item.get("question_text", "")).strip(),
                options=options,
                hint_available=True,
                correct_answer=str(item.get("correct_answer", "")).strip(),
                rubric=str(item.get("rubric", "")).strip(),
                card_index=idx,
            )
        )

    if not questions:
        raise HTTPException(
            status_code=502,
            detail="AI returned zero questions. Please try again.",
        )

    return questions


def _to_response_question(stored: AIQuizQuestionStored) -> AIQuizQuestionResponse:
    """Strip server-only fields before returning to the client."""
    return AIQuizQuestionResponse(
        card_id=stored.card_id,
        question_type=stored.question_type,
        question_text=stored.question_text,
        options=stored.options,
        hint_available=stored.hint_available,
        card_index=stored.card_index,
    )


# ---------------------------------------------------------------------------
# POST /start-ai
# ---------------------------------------------------------------------------


@router.post("/start-ai", response_model=AIQuizStartResponse)
@limiter.limit("5/minute")
async def start_ai_quiz_session(
    request: Request,
    body: AIQuizStartRequest,
    current_user: dict = Depends(track_ai_usage),
) -> AIQuizStartResponse:
    """
    Generate an AI quiz on any topic and return the first question.

    - All questions are generated upfront and cached in MongoDB (24-hour TTL).
    - Does NOT consume the user's monthly message budget.
    - Free tier is always capped at 10 questions.
    - Plus/Pro use their configured ai_quiz_question_count preference (default 10, max 20).
    - Free tier uses Groq/Llama 3.3 70B; Plus gets Gemini Flash; Pro gets Gemini Pro.
    """
    user_id: str = current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="User record not found")

    # Fetch user for preference resolution (subscription tier read from this doc — single source)
    user_doc = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"subscription": 1, "preferences.agent.ai_quiz_question_count": 1},
    )
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    # Single tier source: derived from the already-fetched user_doc (CR-02, CR-03)
    tier: str = user_doc.get("subscription", {}).get("tier", "free")
    effective_count: int = _resolve_question_count(body.question_count, user_doc, tier)

    # Validate tier before calling _generate_questions (fail-fast)
    if tier not in ("free", "plus", "pro"):
        logger.error(f"[ai_quiz] Unknown tier={tier!r} — defaulting to free for quiz routing")
        tier = "free"
    logger.info(f"[ai_quiz] tier={tier} — routing quiz generation")

    # Resolve topic — fall back to a generic label when the frontend couldn't extract one
    effective_topic: str = (body.topic or "").strip() or "the current topic"

    # Generate all questions upfront
    questions: list[AIQuizQuestionStored] = await _generate_questions(
        topic=effective_topic,
        question_count=effective_count,
        language=body.language,
        tier=tier,
        user_id=user_id,
        feature="quiz_from_deck",  # D-09: only current caller of _generate_questions
    )

    # Persist session document with full question set (including correct answers)
    session_id: str = str(uuid4())
    now: datetime = datetime.now(timezone.utc)
    session_doc: dict = {
        "session_id": session_id,
        "user_id": user_id,
        "session_type": "ai",          # discriminator used by /answer handler
        "topic": effective_topic,
        "language": body.language,
        "questions": [q.model_dump() for q in questions],
        "current_index": 0,
        "results": [],
        "status": "active",
        "created_at": now,
    }
    await ai_quiz_sessions_collection.insert_one(session_doc)
    logger.info(
        f"[ai_quiz] Session started: user={user_id}, topic={body.topic!r}, "
        f"questions={len(questions)}, tier={tier}"
    )

    return AIQuizStartResponse(
        session_id=session_id,
        total_questions=len(questions),
        first_question=_to_response_question(questions[0]),
    )


# ---------------------------------------------------------------------------
# Lexical JSON text extraction helper
# ---------------------------------------------------------------------------


def _extract_text_from_lexical_quiz(lexical_state: dict) -> str:
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


# ---------------------------------------------------------------------------
# Quiz question normalisation
# ---------------------------------------------------------------------------

# The book-quiz prompt historically asked for `correct_answer` +
# `incorrect_answers`, while the frontend QuestionnaireModal (shared with
# POST /quiz/generate) reads `question` / `options` / `answer` / `explanation`.
# The mismatch made every option list render empty. We normalise server-side so
# the wire contract holds regardless of which prompt version Langfuse serves.
_QUESTION_KEYS: tuple[str, ...] = ("question", "question_text", "prompt", "text")
_ANSWER_KEYS: tuple[str, ...] = ("answer", "correct_answer", "correctAnswer")
_OPTIONS_KEYS: tuple[str, ...] = ("options", "choices", "alternatives", "answers")
_DISTRACTOR_KEYS: tuple[str, ...] = (
    "incorrect_answers",
    "incorrectAnswers",
    "distractors",
    "wrong_answers",
)
_EXPLANATION_KEYS: tuple[str, ...] = ("explanation", "rationale", "reason")


def _first_str(raw: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value found under `keys`, else ""."""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _string_list(value: object) -> list[str]:
    """Coerce an LLM-supplied value into a list of non-empty trimmed strings."""
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = item.strip() if isinstance(item, str) else ""
        if text:
            items.append(text)
    return items


def _normalize_quiz_question(raw: object) -> Optional[GeneratedQuizQuestion]:
    """Map one LLM-produced question onto the canonical frontend shape.

    Accepts either the `options`/`answer` shape or the
    `correct_answer`/`incorrect_answers` shape. Returns None when the entry
    cannot produce a usable multiple-choice question (missing text, or fewer
    than two distinct options) so malformed items are dropped instead of
    rendering as an empty option list.
    """
    if not isinstance(raw, dict):
        return None

    question = _first_str(raw, _QUESTION_KEYS)
    if not question:
        return None

    answer = _first_str(raw, _ANSWER_KEYS)

    options: list[str] = []
    for key in _OPTIONS_KEYS:
        options = _string_list(raw.get(key))
        if options:
            break

    if not options:
        distractors: list[str] = []
        for key in _DISTRACTOR_KEYS:
            distractors = _string_list(raw.get(key))
            if distractors:
                break
        if answer and distractors:
            options = [answer, *distractors]
            random.shuffle(options)

    # De-duplicate while preserving order (an LLM occasionally repeats a choice).
    seen: set[str] = set()
    options = [opt for opt in options if not (opt in seen or seen.add(opt))]

    if answer and answer not in options:
        # The answer must be selectable, otherwise the question is unanswerable.
        options.append(answer)
        random.shuffle(options)

    if len(options) < 2:
        return None
    if not answer:
        return None

    difficulty = _first_str(raw, ("difficulty",))
    return GeneratedQuizQuestion(
        question=question,
        options=options,
        answer=answer,
        explanation=_first_str(raw, _EXPLANATION_KEYS) or None,
        difficulty=difficulty or None,
    )


# ---------------------------------------------------------------------------
# POST /generate-from-book — Plus+ only
# ---------------------------------------------------------------------------


@router.post("/generate-from-book", response_model=GenerateQuizFromBookResponse)
async def generate_quiz_from_book(
    body: GenerateQuizFromBookRequest,
    current_user: dict = Depends(track_ai_usage),
    tier: str = Depends(get_subscription_tier),
) -> GenerateQuizFromBookResponse:
    """Generate quiz questions from full book content. Plus+ only."""
    if tier == "free":
        raise HTTPException(status_code=403, detail="Book-wide quiz generation requires Plus or Pro.")

    user_id: str = current_user.get("user_id", "")

    try:
        book = await books_collection.find_one({"_id": ObjectId(body.book_id), "deleted_at": None})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid book ID.")

    if not book or book.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Book not found.")

    raw_content: str = book.get("full_content", "")
    try:
        lexical_state = json.loads(raw_content)
        plain_text = _extract_text_from_lexical_quiz(lexical_state)
    except (json.JSONDecodeError, KeyError):
        plain_text = raw_content

    _MAX_BOOK_CHARS = 50_000
    if len(plain_text) > _MAX_BOOK_CHARS:
        logger.warning(f"[generate_quiz_from_book] Book truncated {len(plain_text)} → {_MAX_BOOK_CHARS}")
        plain_text = plain_text[:_MAX_BOOK_CHARS]

    if not plain_text.strip():
        raise HTTPException(status_code=400, detail="Book has no text content to analyze.")

    question_limit = 20 if tier == "plus" else None  # Pro = unlimited

    llm_client = get_client_for_tier(tier)
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    system_prompt = prompt_manager.get_prompt(
        "nowry-quiz-from-book",
        question_limit=question_limit if question_limit else "as many as appropriate",
    )
    system_prompt = f"{system_prompt}\n\n{_prompts.MATH_NOTATION_INSTRUCTION}"
    user_prompt = f"Book content:\n{plain_text}"

    client = get_langfuse_client()
    model_name = TIER_MODEL_NAMES.get(tier, TIER_MODEL_NAMES["free"])
    trace_metadata = {"feature": "quiz_from_book", "tier": tier, "user_id": user_id, "model": model_name}

    raw_text: str = ""
    for attempt in range(1, 3):
        try:
            combined_prompt = f"{system_prompt}\n\n{user_prompt}"
            if client:
                try:
                    with propagate_attributes(
                        user_id=user_id,
                        trace_name="quiz_from_book",
                        metadata=trace_metadata,
                        tags=["quiz_from_book", tier],
                    ):
                        with client.start_as_current_observation(
                            name="quiz_from_book",
                            as_type="generation",
                            model=model_name,
                            input=[{"role": "user", "content": combined_prompt}],
                            model_parameters={"question_limit": question_limit},
                        ) as generation:
                            completion = llm_client.request(combined_prompt)
                            raw_text = (completion.choices[0].message.content or "").strip()
                            # D-13: full output, no truncation. Gemini wrapper exposes no usage -> None.
                            generation.update(output=raw_text, usage_details=None)
                except Exception as langfuse_exc:
                    logger.warning(
                        f"[generate_quiz_from_book] Langfuse tracing failed, continuing without trace: {langfuse_exc}"
                    )
                    completion = llm_client.request(combined_prompt)
                    raw_text = (completion.choices[0].message.content or "").strip()
            else:
                completion = llm_client.request(combined_prompt)
                raw_text = (completion.choices[0].message.content or "").strip()

            if raw_text:
                break
            logger.warning(f"[generate_quiz_from_book] Empty response attempt {attempt}")
        except Exception as exc:
            logger.warning(f"[generate_quiz_from_book] LLM error attempt {attempt}: {exc}")

    if not raw_text:
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(line for line in lines if not line.startswith("```")).strip()

    try:
        parsed: list = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise ValueError("Expected JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(f"[generate_quiz_from_book] Malformed JSON: {exc}\nRaw: {raw_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned unexpected format. Please try again.")

    if question_limit:
        parsed = parsed[:question_limit]

    questions: list[GeneratedQuizQuestion] = []
    for item in parsed:
        normalized = _normalize_quiz_question(item)
        if normalized is not None:
            questions.append(normalized)

    dropped = len(parsed) - len(questions)
    if dropped:
        logger.warning(
            f"[generate_quiz_from_book] Dropped {dropped}/{len(parsed)} malformed questions "
            f"(missing question text or fewer than 2 options)."
        )

    if not questions:
        logger.error(
            f"[generate_quiz_from_book] No usable questions after normalisation. Raw: {raw_text[:500]}"
        )
        raise HTTPException(status_code=502, detail="AI returned unexpected format. Please try again.")

    return GenerateQuizFromBookResponse(questions=questions)


# ---------------------------------------------------------------------------
# POST /analyze-deck — Pro only
# ---------------------------------------------------------------------------


@router.post("/analyze-deck", response_model=DeckQuizAnalysisResponse)
async def analyze_deck_for_quiz(
    deck_id: str,
    current_user: dict = Depends(track_ai_usage),
    tier: str = Depends(get_subscription_tier),
) -> DeckQuizAnalysisResponse:
    """Pro-only: analyze a full deck and generate quiz questions from its content."""
    if tier != "pro":
        raise HTTPException(status_code=403, detail="Deck quiz analysis requires Pro.")

    user_id: str = current_user.get("user_id", "")

    try:
        deck_oid = ObjectId(deck_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid deck ID.")

    # Verify deck ownership
    deck = await decks_collection.find_one({"_id": deck_oid, "user_id": user_id, "deleted_at": None})
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found.")

    # Load all cards in batches of 25 — use $or for ObjectId/string deck_id mismatch
    # (same pattern as POST /card/analyze-deck in cards.py — CARD-03)
    deck_or = [{"deck_id": deck_oid}, {"deck_id": str(deck_oid)}]
    base_query = {"user_id": user_id, "deleted_at": None, "$or": deck_or}

    all_cards: list = []
    skip = 0
    BATCH_SIZE = 25
    while True:
        batch = await cards_collection.find(base_query).skip(skip).to_list(length=BATCH_SIZE)
        if not batch:
            break
        all_cards.extend(batch)
        skip += BATCH_SIZE
        if len(batch) < BATCH_SIZE:
            break

    if not all_cards:
        return DeckQuizAnalysisResponse(quiz_questions=[], card_count=0, deck_id=deck_id)

    card_count = len(all_cards)

    # Process in batches of 25 — generate quiz questions per batch then combine
    llm_client = get_client_for_tier("pro")  # always Gemini Pro for deck analysis — Pro-only
    if llm_client is None:
        raise HTTPException(status_code=503, detail="AI service unavailable. API key not configured.")

    all_questions: list = []
    for batch_start in range(0, card_count, BATCH_SIZE):
        batch = all_cards[batch_start: batch_start + BATCH_SIZE]
        batch_text = "\n".join(
            f"Card {i + 1}: Front: {c.get('title', '')} | Back: {c.get('content', '')}"
            for i, c in enumerate(batch)
        )
        questions_per_batch = max(1, len(batch) // 2)  # ~2 questions per 4 cards

        system_prompt = prompt_manager.get_prompt("nowry-quiz-from-deck", questions_per_batch=questions_per_batch)
        user_prompt = f"Deck name: {deck.get('name', 'Unknown')}\n\nCards:\n{batch_text}"

        raw_text: str = ""
        for attempt in range(1, 3):
            try:
                combined_prompt = f"{system_prompt}\n\n{user_prompt}"
                completion = llm_client.request(combined_prompt)
                raw_text = (completion.choices[0].message.content or "").strip()
                if raw_text:
                    break
                logger.warning(f"[analyze_deck_for_quiz] Empty response attempt {attempt}")
            except Exception as exc:
                logger.warning(f"[analyze_deck_for_quiz] LLM error attempt {attempt}: {exc}")

        if not raw_text:
            logger.error(f"[analyze_deck_for_quiz] LLM failed for batch starting at {batch_start}")
            continue

        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(line for line in lines if not line.startswith("```")).strip()

        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                all_questions.extend(str(q) for q in parsed if q)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(f"[analyze_deck_for_quiz] Malformed JSON batch: {exc}\nRaw: {raw_text[:300]}")
            # Continue to next batch rather than failing entire request

    if not all_questions:
        raise HTTPException(status_code=502, detail="AI service error. Please try again.")

    return DeckQuizAnalysisResponse(
        quiz_questions=all_questions,
        card_count=card_count,
        deck_id=deck_id,
    )
