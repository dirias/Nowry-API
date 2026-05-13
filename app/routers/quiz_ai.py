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
from uuid import uuid4

from groq import Groq
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request

from app.ai_orchestrator.llm_clients.gemini_client import Gemini_client
from app.auth.dependencies import get_subscription_tier, track_ai_usage
from app.config.database import ai_quiz_sessions_collection, users_collection
from app.config.subscription_plans import SUBSCRIPTION_PLANS, SubscriptionTier
from app.core.limiter import limiter
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
    tier: SubscriptionTier,
) -> int:
    """
    Return the effective question count for this session.

    Free tier is always capped at 10.
    Plus/Pro respect the user-configured preference (ai_quiz_question_count),
    clamped to the requested value and the hard maximum of 20.
    """
    if tier == SubscriptionTier.FREE:
        return 10

    configured: int = (
        user.get("preferences", {})
        .get("agent", {})
        .get("ai_quiz_question_count", 10)
    )
    # The user-configured value is the session maximum; the caller may request fewer.
    effective = min(requested, configured, 20)
    return max(effective, 1)


_GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def _get_groq_client() -> Groq:
    """Return a configured Groq client. Raises RuntimeError if key missing."""
    api_key: str = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY environment variable is not set")
    return Groq(api_key=api_key)


def _get_llm_client_for_tier(tier: str):
    """Return the appropriate LLM client for the given subscription tier.

    Centralizes all tier-to-model dispatch for quiz AI (mirrors orchestrator.py pattern).

    Returns:
        - free: Groq client (Llama 3.3 70B, zero marginal cost)
        - plus: Gemini Flash client
        - pro:  Gemini Pro client
    Raises:
        RuntimeError: if GROQ_API_KEY is missing (free tier)
        ValueError: if GEMINI_API_KEY is missing (plus/pro tier)
        ValueError: if tier is an unexpected value (fail-fast)
    """
    if tier == "free":
        return _get_groq_client()
    elif tier == "plus":
        return Gemini_client("models/gemini-flash-latest")
    elif tier == "pro":
        return Gemini_client("models/gemini-pro-latest")
    else:
        raise ValueError(f"Unknown subscription tier: {tier!r}. Expected 'free', 'plus', or 'pro'.")


async def _generate_questions(
    topic: str,
    question_count: int,
    language: str,
    tier: str,
) -> list[AIQuizQuestionStored]:
    """
    Call the tier-appropriate LLM to generate `question_count` questions on `topic`.

    The model is asked to return a JSON array of question objects. Each object
    must have the fields defined in AIQuizQuestionStored. We request three
    question types in a balanced mix to ensure variety.

    Returns a list of AIQuizQuestionStored instances.
    Raises HTTPException(502) on LLM API failure or malformed JSON.
    """
    llm_client = _get_llm_client_for_tier(tier)

    lang_name: str = _LANGUAGE_NAMES.get(language.split("-")[0].lower(), "English")

    system_prompt: str = (
        "You are an expert quiz designer. "
        "Generate a set of study quiz questions on the given topic. "
        f"All question text and answer text must be written in {lang_name}. "
        "When the topic involves a language with non-latin script (Japanese, Chinese, Korean, Arabic, etc.), "
        "include romanisation or pronunciation guides in parentheses wherever helpful to the learner.\n\n"
        "Rules:\n"
        "- Return ONLY a valid JSON array — no markdown, no prose, no code fences.\n"
        "- Each element must be an object with exactly these fields:\n"
        "  question_type: one of 'fill_in_blank' | 'multiple_choice' | 'short_answer'\n"
        "  question_text: the full question string\n"
        "  options: an array of 4 strings for multiple_choice, null otherwise\n"
        "  correct_answer: the primary expected answer string\n"
        "  rubric: a concise grading guide (1-3 sentences) written as instructions to an evaluator. "
        "Default stance is PERMISSIVE — describe what disqualifies an answer, not what qualifies it. "
        "Always accept: any correct script system (kanji, hiragana, katakana, romaji, pinyin, etc.), "
        "reasonable typos or misspellings that don't change meaning, equivalent forms in any language, "
        "and any phrasing that demonstrates the student knows the answer. "
        "Only describe restrictions when the question explicitly requires a specific form or register. "
        "Example: 'Accept any correct past tense form in any script — 食べた, tabeta, and tabemashita "
        "are all valid. Only reject if the student gives a non-past form or the wrong verb entirely. "
        "Typos like tabetta are fine.'\n"
        "- Distribute question types: roughly 1/3 each.\n"
        "- For multiple_choice: include the correct answer as one of the 4 options.\n"
        "- Make questions genuinely educational — not trivially easy.\n"
        "- VARIETY IS CRITICAL: each quiz session must feel different. "
        "Deliberately avoid the most common or obvious examples for the topic — "
        "choose a wide, randomised spread from across the full topic range. "
        "If the topic is a vocabulary or word list (e.g. JLPT verbs, Spanish irregular verbs), "
        "do NOT default to the most frequent/basic items. Pick an eclectic mix including "
        "mid-frequency and less obvious entries so repeated sessions feel fresh.\n"
        "- Never repeat the same concept in two questions within this session.\n"
        "- When a fill_in_blank question requires a specific form, variant, or register "
        "(e.g. a verb tense, grammatical case, chemical symbol, abbreviated form), state it "
        "explicitly in the question_text so the student knows exactly what is expected. "
        "The correct_answer must be precisely the form the question asks for — they must always agree.\n"
        "- Output must be a JSON array only. No other text whatsoever."
    )

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
        try:
            if tier == "free":
                # Groq: native chat.completions.create interface
                completion = llm_client.chat.completions.create(
                    model=_GROQ_MODEL,
                    max_tokens=4096,
                    temperature=0.7,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                choice = completion.choices[0]
                finish_reason: str = choice.finish_reason or "unknown"
                raw_text = (choice.message.content or "").strip()
            else:
                # Gemini: combine prompts, use request() — returns _GeminiResponseShim
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
    tier: str = Depends(get_subscription_tier),
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

    # Fetch user for tier + preference resolution
    user_doc = await users_collection.find_one(
        {"_id": ObjectId(user_id)},
        {"subscription": 1, "preferences.agent.ai_quiz_question_count": 1},
    )
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    subscription_tier: SubscriptionTier = _resolve_tier(user_doc)
    effective_count: int = _resolve_question_count(body.question_count, user_doc, subscription_tier)

    # Validate tier before calling _generate_questions (fail-fast)
    if tier not in ("free", "plus", "pro"):
        logger.error(f"[ai_quiz] Unknown tier={tier!r} — cannot route to LLM")
        raise HTTPException(
            status_code=500,
            detail="AI service is not configured. Contact support.",
        )
    logger.info(f"[ai_quiz] tier={tier} — routing quiz generation")

    # Resolve topic — fall back to a generic label when the frontend couldn't extract one
    effective_topic: str = (body.topic or "").strip() or "the current topic"

    # Generate all questions upfront
    questions: list[AIQuizQuestionStored] = await _generate_questions(
        topic=effective_topic,
        question_count=effective_count,
        language=body.language,
        tier=tier,
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
