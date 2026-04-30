"""
Active Study Partner — AI-powered quiz session router.

Endpoints:
  GET  /v1/assistant/quiz/decks   — List quizzable decks with due/new counts
  POST /v1/assistant/quiz/start   — Initialize a session, get first AI question
  POST /v1/assistant/quiz/answer  — Submit answer, get AI evaluation + next question
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from groq import Groq
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.firebase_auth import get_firebase_user
from app.config.database import ai_quiz_sessions_collection, cards_collection, decks_collection, quiz_sessions_collection, study_sessions_collection
from app.core.limiter import limiter
from app.models.quiz import (
    AIQuizQuestionResponse,
    AIQuizQuestionStored,
    ConversationMessage,
    QuizDeckInfo,
    QuizDecksResponse,
    QuizQuestion,
    QuizSummary,
    StartQuizRequest,
    StartQuizResponse,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(
    tags=["quiz"],
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def _get_groq_client() -> Groq:
    """Return a configured Groq client. Raises RuntimeError if key missing."""
    api_key: str = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY environment variable is not set")
    return Groq(api_key=api_key)


def _extract_card_fields(card: dict) -> dict:
    """
    Extract meaningful fields from a card document using the actual StudyCard schema.
    Cards use 'title' (the term) and 'content' (the definition/explanation).
    Falls back to 'front'/'back' for legacy cards.
    """
    fields: dict = {}

    title = card.get("title", "").strip()
    content = card.get("content", "").strip()
    explanation = card.get("explanation", "")

    if title:
        fields["term"] = title
    if content:
        fields["definition"] = content
    if explanation:
        fields["explanation"] = explanation

    # Legacy fallback
    if not fields:
        front = card.get("front", "").strip()
        back = card.get("back", "").strip()
        if front:
            fields["term"] = front
        if back:
            fields["definition"] = back

    return fields


def _select_question_type(
    fields: dict,
    deck_card_count: int,
) -> Literal["definition", "contextual", "comparison", "fill_in", "open_recall"]:
    """
    Weighted random selection of question type based on available card fields.

    Weights:
    - definition (weight 4): always available — ask what the term means
    - open_recall (weight 3): always available — tell me everything about X
    - contextual (weight 2): when definition field exists — use it in a sentence
    - fill_in (weight 1): when definition field exists — complete the sentence
    """
    has_definition = "definition" in fields

    pool: list[tuple[str, int]] = [
        ("definition", 4),
        ("open_recall", 3),
    ]
    if has_definition:
        pool.append(("contextual", 2))
        pool.append(("fill_in", 1))

    types, weights = zip(*pool)
    selected: str = random.choices(list(types), weights=list(weights), k=1)[0]
    return selected  # type: ignore[return-value]


async def _generate_question_text(
    fields: dict,
    question_type: str,
    client: Groq,
) -> str:
    """Call Groq to generate a question string. Returns fallback text on failure."""
    if not fields:
        logger.warning("[quiz] Card has no extractable fields — skipping AI call")
        return "What can you tell me about this card?"
    fields_json = json.dumps(fields, ensure_ascii=False, default=str)
    try:
        completion = client.chat.completions.create(
            model=_GROQ_MODEL,
            max_tokens=256,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Generate a single {question_type} study question from the flashcard below. "
                        "Return ONLY the question text — nothing else.\n\n"
                        "Rules:\n"
                        "- Name the exact term — never say 'this term' or 'this card'\n"
                        "- For Japanese: write kanji with furigana in parentheses e.g. 精々(せいぜい)\n"
                        "- Sound like a person asking, not a template\n\n"
                        "BAD: 'What is the definition of the term on this card?'\n"
                        "GOOD: 'What does 精々(せいぜい) mean?'\n\n"
                        "BAD: 'Use the term in a sentence.'\n"
                        "GOOD: 'Use 精々(せいぜい) in a sentence that shows you understand it means at most or to the limit.'"
                    ),
                },
                {"role": "user", "content": f"Card fields: {fields_json}"},
            ],
        )
        text: str = (completion.choices[0].message.content or "").strip()
        return text if text else "What is the definition of this term?"
    except Exception as exc:
        logger.error(f"[quiz] Groq question generation failed: {exc}")
        return "What can you tell me about this card?"


async def _build_question(
    card: dict,
    card_index: int,
    total_deck_cards: int,
    client: Groq,
    comparison_card_id: str | None = None,
) -> QuizQuestion:
    """Generate a QuizQuestion for a single card."""
    card_id = str(card["_id"])
    fields: dict = _extract_card_fields(card)

    question_type = _select_question_type(
        fields=fields,
        deck_card_count=total_deck_cards,
    )

    question_text = await _generate_question_text(fields, question_type, client)

    return QuizQuestion(
        card_id=card_id,
        question_type=question_type,
        question_text=question_text,
        card_index=card_index,
        comparison_card_id=comparison_card_id,
    )


async def _evaluate_answer(
    card: dict,
    question_text: str,
    question_type: str,
    user_answer: str,
    attempt_number: int,
    client: Groq,
    conversation_history: list[ConversationMessage] | None = None,
) -> dict:
    """
    Call Claude to evaluate a student's answer.
    Returns a dict with keys: evaluation, feedback_message, hint, revealed_answer.
    Raises HTTPException(500) if the response is malformed JSON.
    """
    fields: dict = _extract_card_fields(card)
    fields_json = json.dumps(fields, ensure_ascii=False, default=str)

    term = fields.get("term", "")
    definition = fields.get("definition", "")

    system_prompt = f"""You are a knowledgeable, adaptive study tutor evaluating a student's flashcard answer.

CURRENT CARD: term={term!r}, expected answer={definition!r}
ORIGINAL QUESTION: {question_text}
ATTEMPT NUMBER: {attempt_number}

FIRST — infer the knowledge domain from the card content (e.g. language learning, cloud certification, cooking, fitness, history, medicine, music, math, etc.). Your entire response style — feedback depth, analogy type, terminology — must match that domain naturally. Do not default to language-learning examples or techniques for non-language cards.

Your job: read the student's latest message and decide what kind of response is needed.

TWO RESPONSE TYPES — you must pick one:

TYPE A — "followup": The student is asking a question, requesting an explanation, saying they don't know, or continuing the conversation. Respond conversationally — teach, explain, give domain-appropriate examples. Always end by re-inviting them to answer, e.g. "Give it a shot."

TYPE B — "evaluation": The student is attempting to answer the question. Evaluate their answer against the expected answer.

EVALUATION RULES:
- Be permissive: the goal is to assess understanding, not exact string matching.
- Mark CORRECT when the student clearly demonstrates they understand the concept, regardless of phrasing.
- For language cards: accept any valid script (kanji, romaji, pinyin, cyrillic, etc.) for the same word.
- Mark PARTIAL when the student shows understanding but has a genuine factual or conceptual error.
- Mark INCORRECT only when the answer is factually or conceptually wrong.
- For multiple_choice: only accept the correct option.

FEEDBACK RULES:
- Never open with filler ("Great job!", "Let's explore", "Don't worry", "That's okay").
- Correct: one sentence confirmation + one optional insight relevant to the domain.
- Incorrect/partial: state what the correct answer actually IS, explain what it means in domain context, and briefly contrast it with what the student said.
- Hints (attempt 1, incorrect only): teach the concept directly using a domain-appropriate explanation or analogy. Never use examples from competing platforms or unrelated domains.
- Never write "think about the meaning" or restate the question.

Respond ONLY with raw JSON (no markdown):
{{
  "response_type": "followup" | "evaluation",
  "evaluation": "correct" | "partial" | "incorrect" | null,
  "feedback_message": "<your response — conversational if followup, evaluative if evaluation>",
  "hint": "<concrete domain-appropriate hint if evaluation+attempt 1+incorrect — null otherwise>",
  "revealed_answer": "<full expected answer if evaluation+not correct+attempt>=2 — null otherwise>"
}}"""

    # Build message array with full conversation history
    history = conversation_history or []
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": user_answer})

    try:
        completion = client.chat.completions.create(
            model=_GROQ_MODEL,
            max_tokens=512,
            temperature=0.6,
            messages=messages,
        )
        raw_text: str = (completion.choices[0].message.content or "").strip()

        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        parsed: dict = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error(f"[quiz] Claude evaluation returned malformed JSON: {exc}")
        raise HTTPException(
            status_code=500,
            detail="AI evaluation returned an unexpected format. Please try again.",
        )
    except Exception as exc:
        logger.error(f"[quiz] Claude evaluation call failed: {exc}")
        raise HTTPException(
            status_code=502,
            detail="AI service error during answer evaluation.",
        )

    # Validate required keys with safe defaults
    response_type: str = parsed.get("response_type", "evaluation")
    if response_type not in ("followup", "evaluation"):
        response_type = "evaluation"

    evaluation: str | None = parsed.get("evaluation")
    if response_type == "evaluation" and evaluation not in ("correct", "partial", "incorrect"):
        evaluation = "incorrect"
    if response_type == "followup":
        evaluation = None

    return {
        "response_type": response_type,
        "evaluation": evaluation,
        "feedback_message": parsed.get("feedback_message", ""),
        "hint": parsed.get("hint"),
        "revealed_answer": parsed.get("revealed_answer"),
    }


def _compute_summary(
    results: list[dict],
    weak_card_terms: list[str],
    source: str | None = None,
) -> QuizSummary:
    """Compute quiz summary statistics from session result list."""
    total = len(results)
    correct = sum(1 for r in results if r.get("evaluation") == "correct")
    partial = sum(1 for r in results if r.get("evaluation") == "partial")
    incorrect = total - correct - partial
    score_percentage = round((correct + partial * 0.5) / total * 100) if total > 0 else 0
    weak_card_ids = [
        r["card_id"] for r in results if r.get("evaluation") == "incorrect"
    ]
    return QuizSummary(
        total_cards=total,
        correct=correct,
        partial=partial,
        incorrect=incorrect,
        score_percentage=score_percentage,
        weak_card_ids=weak_card_ids,
        weak_card_terms=weak_card_terms,
        source=source,
    )


def _deck_field_names(first_card: dict) -> list[str]:
    """Derive field_names from a card using the actual StudyCard schema."""
    return list(_extract_card_fields(first_card).keys())


# ---------------------------------------------------------------------------
# ENDPOINT 1 — GET /decks
# ---------------------------------------------------------------------------


@router.get("/decks", response_model=QuizDecksResponse)
@limiter.limit("30/minute")
async def list_quizzable_decks(
    request: Request,
    current_user: dict = Depends(get_firebase_user),
) -> QuizDecksResponse:
    """Return user's decks that have at least one card, with due and new card counts."""
    user_id: str = current_user.get("user_id", "")
    now: datetime = datetime.now(timezone.utc)

    raw_decks = await decks_collection.find(
        {"user_id": user_id, "deleted_at": None}
    ).to_list(length=100)

    quiz_decks: list[QuizDeckInfo] = []

    for deck in raw_decks:
        deck_id_str: str = str(deck["_id"])

        try:
            deck_oid = ObjectId(deck_id_str)
        except Exception:
            continue

        deck_filter = {
            "deck_id": {"$in": [deck_oid, deck_id_str]},
            "deleted_at": None,
        }

        # Count due cards (previously reviewed, now past next_review)
        due_count: int = await cards_collection.count_documents({
            **deck_filter,
            "last_reviewed": {"$ne": None},
            "next_review": {"$lte": now},
        })

        # Count new cards (never reviewed)
        new_count: int = await cards_collection.count_documents({
            **deck_filter,
            "last_reviewed": None,
        })

        total = due_count + new_count
        if total == 0:
            continue

        # Derive field names from the first card in this deck
        first_card = await cards_collection.find_one(deck_filter)
        field_names: list[str] = _deck_field_names(first_card) if first_card else []

        quiz_decks.append(
            QuizDeckInfo(
                deck_id=deck_id_str,
                name=deck.get("name", "Untitled Deck"),
                field_names=field_names,
                due_count=due_count,
                new_count=new_count,
            )
        )

    return QuizDecksResponse(decks=quiz_decks)


# ---------------------------------------------------------------------------
# ENDPOINT 2 — POST /start
# ---------------------------------------------------------------------------


@router.post("/start", response_model=StartQuizResponse)
@limiter.limit("5/minute")
async def start_quiz_session(
    request: Request,
    body: StartQuizRequest,
    current_user: dict = Depends(get_firebase_user),
) -> StartQuizResponse:
    """Initialize a quiz session and return the first AI-generated question."""
    user_id: str = current_user.get("user_id", "")
    now: datetime = datetime.now(timezone.utc)

    # Validate deck ownership
    try:
        deck_oid = ObjectId(body.deck_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Deck not found")

    deck = await decks_collection.find_one(
        {"_id": deck_oid, "user_id": user_id, "deleted_at": None}
    )
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    deck_filter = {
        "deck_id": {"$in": [deck_oid, body.deck_id]},
        "user_id": user_id,
        "deleted_at": None,
    }

    # Fetch cards — due first if prioritize_due=True
    cards: list[dict] = []

    if body.prioritize_due:
        due_cards = await cards_collection.find({
            **deck_filter,
            "last_reviewed": {"$ne": None},
            "next_review": {"$lte": now},
        }).to_list(length=body.card_count)
        cards.extend(due_cards)

        remaining = body.card_count - len(cards)
        if remaining > 0:
            new_cards = await cards_collection.find({
                **deck_filter,
                "last_reviewed": None,
            }).to_list(length=remaining)
            cards.extend(new_cards)
    else:
        cards = await cards_collection.find(deck_filter).to_list(length=body.card_count)

    if not cards:
        raise HTTPException(
            status_code=422,
            detail="This deck has no cards available for a quiz session.",
        )

    # Generate first question via Groq (before session insert so we can store it)
    try:
        client = _get_groq_client()
    except RuntimeError:
        logger.error("[quiz] GROQ_API_KEY is not configured")
        raise HTTPException(status_code=500, detail="AI service is not configured. Contact support.")

    card_ids: list[str] = [str(c["_id"]) for c in cards]

    first_question = await _build_question(
        card=cards[0],
        card_index=0,
        total_deck_cards=len(cards),
        client=client,
    )

    # Create session document in MongoDB — store the server-generated question text
    # so /answer can use it instead of trusting the client payload
    session_id: str = str(uuid4())
    session_doc: dict = {
        "session_id": session_id,
        "user_id": user_id,
        "deck_id": body.deck_id,
        "card_ids": card_ids,
        "current_index": 0,
        "results": [],
        "created_at": now,
        "status": "active",
        "current_question": {
            "card_id": first_question.card_id,
            "question_type": first_question.question_type,
            "question_text": first_question.question_text,
        },
    }
    await quiz_sessions_collection.insert_one(session_doc)
    logger.info(f"[quiz] Session started: user={user_id}, deck={body.deck_id}, cards={len(cards)}")

    return StartQuizResponse(
        session_id=session_id,
        total_cards=len(cards),
        first_question=first_question,
    )


# ---------------------------------------------------------------------------
# AI quiz helpers — deck auto-save and answer evaluation
# ---------------------------------------------------------------------------


def _human_readable_date(dt: datetime) -> str:
    """Return a date string like 'Apr 21 2026' from a datetime."""
    return dt.strftime("%b %d %Y")


async def _save_ai_quiz_as_deck(
    user_id: str,
    topic: str,
    questions: list[dict],
) -> str:
    """
    Persist the completed AI quiz as a new deck in MongoDB.

    Returns the string ID of the created deck document.
    """
    from app.config.database import decks_collection as _decks

    now: datetime = datetime.now(timezone.utc)
    tag: str = f"{_human_readable_date(now)} · {topic}"

    cards: list[dict] = [
        {
            "front": q.get("question_text", ""),
            "back": q.get("correct_answer", ""),
        }
        for q in questions
    ]

    deck_doc: dict = {
        "user_id": user_id,
        "name": topic,
        "source": "ai_quiz",
        "tag": tag,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
        "cards": cards,
    }

    result = await _decks.insert_one(deck_doc)
    return str(result.inserted_id)


async def _evaluate_ai_answer(
    question: dict,
    user_answer: str,
    attempt_number: int,
    client: Groq,
    conversation_history: list[ConversationMessage] | None = None,
    language: str = "en",
    rubric: str = "",
) -> dict:
    """
    Evaluate a user's answer against an AI quiz question's correct answer.

    Uses the same Groq-based evaluation engine as the deck quiz, but passes
    the pre-generated correct answer directly instead of deriving it from a card.
    Returns a dict with keys: response_type, evaluation, feedback_message, hint, revealed_answer.
    """
    question_text: str = question.get("question_text", "")
    correct_answer: str = question.get("correct_answer", "")
    q_type: str = question.get("question_type", "short_answer")
    _LANG_NAMES: dict[str, str] = {
        "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
        "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "it": "Italian", "ar": "Arabic",
    }
    lang_name: str = _LANG_NAMES.get(language.split("-")[0].lower(), "English")

    rubric_section: str = f"\nGRADING RUBRIC: {rubric}" if rubric else ""

    system_prompt: str = f"""You are a knowledgeable, adaptive study tutor evaluating a student's quiz answer.

LANGUAGE: Respond entirely in {lang_name}. All feedback, hints, and revealed answers must be in {lang_name}. When the subject matter itself uses another language or script (e.g. Japanese terms, AWS service names, chemical formulas), preserve those exactly as-is.

QUESTION: {question_text}
EXPECTED ANSWER: {correct_answer}{rubric_section}
QUESTION TYPE: {q_type}
ATTEMPT NUMBER: {attempt_number}

FIRST — infer the knowledge domain from the question and expected answer (e.g. cloud computing, language learning, cooking, fitness, history, medicine, music, mathematics, etc.). Your entire response — feedback tone, analogy type, domain vocabulary — must match that domain. Never default to language-learning examples or script-conversion logic for non-language content.

TWO RESPONSE TYPES — pick one:

TYPE A — "followup": The student is asking a question, requesting explanation, saying they don't know, or continuing the conversation. Teach and explain using domain-appropriate depth. Always end by re-inviting them to answer, e.g. "Give it a shot."

TYPE B — "evaluation": The student is attempting to answer. Evaluate against the expected answer.

EVALUATION RULES:
1. GRADING RUBRIC — If one is provided above, follow it as the highest priority.
2. Be permissive: mark CORRECT when the student demonstrates understanding, regardless of exact phrasing.
3. Accept equivalent forms: synonyms, paraphrases, valid alternative scripts, reasonable typos that don't change meaning.
4. For language cards specifically: accept any valid script representation of the same word (kanji/kana/romaji, pinyin/hanzi, cyrillic/latin, etc.).
5. PARTIAL CREDIT: the student shows understanding but has a genuine factual or conceptual error.
6. INCORRECT: only when the answer is factually or conceptually wrong — not just differently expressed.
7. MULTIPLE CHOICE: accept only the correct option.

FEEDBACK RULES:
- Never open with filler ("Great job!", "Let's explore", "Don't worry", "Nice try").
- Correct: one sentence confirmation + one optional domain-relevant insight.
- Incorrect/partial: state what the correct answer IS, explain what it means in the domain context, and contrast with what the student said.
- Hints (attempt 1, incorrect only): teach the concept directly using domain-appropriate explanation. Use a concrete analogy from within the same domain if it helps clarity. Never reference competing services, unrelated platforms, or examples from a different subject area.
- Never restate the question or write "think about the meaning".

Respond ONLY with raw JSON (no markdown):
{{
  "response_type": "followup" | "evaluation",
  "evaluation": "correct" | "partial" | "incorrect" | null,
  "feedback_message": "<your response>",
  "hint": "<domain-appropriate hint if evaluation+attempt 1+incorrect — null otherwise>",
  "revealed_answer": "<full expected answer if evaluation+not correct+attempt>=2 — null otherwise>"
}}"""

    history = conversation_history or []
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in history:
        messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": user_answer})

    try:
        completion = client.chat.completions.create(
            model=_GROQ_MODEL,
            max_tokens=512,
            temperature=0.6,
            messages=messages,
        )
        raw_text: str = (completion.choices[0].message.content or "").strip()

        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        parsed: dict = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error(f"[ai_quiz] Groq evaluation returned malformed JSON: {exc}")
        raise HTTPException(
            status_code=500,
            detail="AI evaluation returned an unexpected format. Please try again.",
        )
    except Exception as exc:
        logger.error(f"[ai_quiz] Groq evaluation call failed: {exc}")
        raise HTTPException(
            status_code=502,
            detail="AI service error during answer evaluation.",
        )

    response_type: str = parsed.get("response_type", "evaluation")
    if response_type not in ("followup", "evaluation"):
        response_type = "evaluation"

    evaluation: str | None = parsed.get("evaluation")
    if response_type == "evaluation" and evaluation not in ("correct", "partial", "incorrect"):
        evaluation = "incorrect"
    if response_type == "followup":
        evaluation = None

    return {
        "response_type": response_type,
        "evaluation": evaluation,
        "feedback_message": parsed.get("feedback_message", ""),
        "hint": parsed.get("hint"),
        "revealed_answer": parsed.get("revealed_answer"),
    }


async def _write_study_session(
    user_id: str,
    session_type: str,
    started_at: datetime,
    total_cards: int,
    correct: int,
    partial: int,
    incorrect: int,
    score_percentage: int,
    cards: list[dict],
    topic: str | None = None,
    deck_id: str | None = None,
    deck_name: str | None = None,
) -> None:
    """
    Persist a completed study session to the permanent study_sessions collection.

    Non-fatal — logs on failure but never raises so a write error never
    breaks the response the user is already waiting for.

    Cards schema:
      AI quiz  → { question_text, correct_answer, question_type, evaluation }
      Deck quiz → { card_id, evaluation }
    """
    now: datetime = datetime.now(timezone.utc).replace(tzinfo=None)
    duration_seconds: int = max(0, int((now - started_at).total_seconds()))
    doc: dict = {
        "user_id": user_id,
        "session_type": session_type,   # "ai_quiz" | "deck_quiz"
        "topic": topic,                 # AI quiz topic string; None for deck quiz
        "deck_id": deck_id,             # deck quiz deck_id; None for AI quiz
        "deck_name": deck_name,         # deck quiz deck name; None for AI quiz
        "total_cards": total_cards,
        "correct": correct,
        "partial": partial,
        "incorrect": incorrect,
        "score_percentage": score_percentage,
        "duration_seconds": duration_seconds,
        "cards": cards,
        "started_at": started_at,
        "completed_at": now,
    }
    try:
        await study_sessions_collection.insert_one(doc)
        logger.info(
            f"[study_sessions] Logged: user={user_id}, type={session_type}, "
            f"score={score_percentage}%, cards={total_cards}, duration={duration_seconds}s"
        )
    except Exception as exc:
        logger.error(f"[study_sessions] Failed to write session log: {exc}")


async def _handle_ai_quiz_answer(
    body: SubmitAnswerRequest,
    user_id: str,
    ai_session: dict,
    client: Groq,
) -> SubmitAnswerResponse:
    """
    Process a /answer submission for an AI quiz session.

    Retrieves the current question from the cached session, evaluates the answer,
    advances the index, and — on completion — auto-saves the session as a deck.
    """
    questions: list[dict] = ai_session.get("questions", [])
    current_index: int = ai_session.get("current_index", 0)

    if current_index >= len(questions):
        raise HTTPException(status_code=409, detail="This quiz session is already complete")

    current_question: dict = questions[current_index]

    # Skip path — bypass LLM, mark incorrect, reveal answer, advance immediately.
    if body.skip:
        eval_result = {
            "response_type": "evaluation",
            "evaluation": "incorrect",
            "feedback_message": "",
            "hint": None,
            "revealed_answer": current_question.get("correct_answer", ""),
        }
    else:
        eval_result = await _evaluate_ai_answer(
            question=current_question,
            user_answer=body.user_answer,
            attempt_number=body.attempt_number,
            client=client,
            conversation_history=body.conversation_history,
            language=ai_session.get("language", "en"),
            rubric=current_question.get("rubric", ""),
        )

    response_type: str = eval_result["response_type"]

    # Follow-up: return without advancing the index
    if response_type == "followup":
        return SubmitAnswerResponse(
            response_type="followup",
            evaluation=None,
            feedback_message=eval_result["feedback_message"],
            revealed_answer=None,
            hint=None,
            score_delta=None,
            next_question=None,
            session_complete=False,
            summary=None,
        )

    evaluation: str = eval_result["evaluation"]  # type: ignore[assignment]
    score_delta: int = 1 if evaluation == "correct" else (-1 if evaluation == "incorrect" else 0)

    # Only advance to the next question when the answer is correct OR the user
    # has already had their second attempt. This gives one retry with a hint on
    # the first incorrect/partial attempt before moving on.
    should_advance: bool = evaluation == "correct" or body.attempt_number >= 2

    result_entry: dict = {
        "card_id": current_question.get("card_id", ""),
        "evaluation": evaluation,
        "score_delta": score_delta,
    }
    new_index: int = current_index + (1 if should_advance else 0)
    is_complete: bool = should_advance and (new_index >= len(questions))

    if should_advance:
        update_doc: dict = {
            "$push": {"results": result_entry},
            "$set": {"current_index": new_index},
        }
        if is_complete:
            update_doc["$set"]["status"] = "complete"
        await ai_quiz_sessions_collection.update_one(
            {"session_id": body.session_id},
            update_doc,
        )

    next_question: AIQuizQuestionResponse | None = None
    summary: QuizSummary | None = None

    if not should_advance:
        # Retry scenario: stay on the current question — next_question stays None
        # so the frontend keeps the current input active.
        pass
    elif not is_complete:
        next_q_raw: dict = questions[new_index]
        next_question = AIQuizQuestionResponse(
            card_id=next_q_raw["card_id"],
            question_type=next_q_raw["question_type"],
            question_text=next_q_raw["question_text"],
            options=next_q_raw.get("options"),
            hint_available=next_q_raw.get("hint_available", True),
            card_index=new_index,
        )
    else:
        all_results: list[dict] = ai_session.get("results", []) + [result_entry]
        total: int = len(all_results)
        correct: int = sum(1 for r in all_results if r.get("evaluation") == "correct")
        partial: int = sum(1 for r in all_results if r.get("evaluation") == "partial")
        incorrect: int = total - correct - partial
        score_percentage: int = round((correct + partial * 0.5) / total * 100) if total > 0 else 0
        weak_card_ids: list[str] = [
            r["card_id"] for r in all_results if r.get("evaluation") == "incorrect"
        ]
        weak_card_terms: list[str] = [
            next(
                (q.get("question_text", "")[:80] for q in questions if q.get("card_id") == wid),
                "",
            )
            for wid in weak_card_ids
        ]

        topic: str = ai_session.get("topic", "AI Quiz")
        logger.info(
            f"[ai_quiz] Session complete: session={body.session_id}, "
            f"score={score_percentage}%"
        )

        summary = QuizSummary(
            total_cards=total,
            correct=correct,
            partial=partial,
            incorrect=incorrect,
            score_percentage=score_percentage,
            weak_card_ids=weak_card_ids,
            weak_card_terms=weak_card_terms,
            source="ai_quiz",
        )

        # Persist permanent session history — join questions with results so
        # we know which question_text + correct_answer each evaluation belongs to.
        question_by_card_id: dict = {q.get("card_id"): q for q in questions}
        session_cards: list[dict] = [
            {
                "question_text": question_by_card_id.get(r["card_id"], {}).get("question_text", ""),
                "correct_answer": question_by_card_id.get(r["card_id"], {}).get("correct_answer", ""),
                "question_type": question_by_card_id.get(r["card_id"], {}).get("question_type", "short_answer"),
                "evaluation": r.get("evaluation", "incorrect"),
            }
            for r in all_results
        ]
        await _write_study_session(
            user_id=user_id,
            session_type="ai_quiz",
            started_at=ai_session.get("created_at", datetime.now(timezone.utc).replace(tzinfo=None)),
            total_cards=total,
            correct=correct,
            partial=partial,
            incorrect=incorrect,
            score_percentage=score_percentage,
            cards=session_cards,
            topic=topic,
        )

    return SubmitAnswerResponse(
        response_type="evaluation",
        evaluation=evaluation,  # type: ignore[arg-type]
        feedback_message=eval_result["feedback_message"],
        revealed_answer=eval_result.get("revealed_answer"),
        hint=eval_result.get("hint"),
        score_delta=score_delta,  # type: ignore[arg-type]
        next_question=next_question,  # type: ignore[arg-type]
        session_complete=is_complete,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# ENDPOINT 3 — POST /answer
# ---------------------------------------------------------------------------


@router.post("/answer", response_model=SubmitAnswerResponse)
@limiter.limit("20/minute")
async def submit_quiz_answer(
    request: Request,
    body: SubmitAnswerRequest,
    current_user: dict = Depends(get_firebase_user),
) -> SubmitAnswerResponse:
    """Submit an answer, receive AI evaluation, and get the next question if any."""
    user_id: str = current_user.get("user_id", "")

    # --- AI quiz fast path ---
    # Check the ai_quiz_sessions collection first. If a match is found, the session
    # belongs to the AI quiz flow (topic-based) and is handled by a separate handler.
    ai_session = await ai_quiz_sessions_collection.find_one(
        {"session_id": body.session_id, "user_id": user_id}
    )
    if ai_session:
        if ai_session.get("status") == "complete":
            raise HTTPException(status_code=409, detail="This quiz session is already complete")
        try:
            groq_client = _get_groq_client()
        except RuntimeError:
            logger.error("[ai_quiz] GROQ_API_KEY is not configured")
            raise HTTPException(
                status_code=500, detail="AI service is not configured. Contact support."
            )
        return await _handle_ai_quiz_answer(body, user_id, ai_session, groq_client)

    # --- Deck quiz path (original behaviour) ---
    # Fetch and validate session — combine ownership into the query to prevent timing attacks
    session = await quiz_sessions_collection.find_one(
        {"session_id": body.session_id, "user_id": user_id}
    )
    if not session:
        raise HTTPException(status_code=404, detail="Quiz session not found")

    if session.get("status") == "complete":
        raise HTTPException(status_code=409, detail="This quiz session is already complete")

    # Fetch the current card's full fields
    try:
        card_oid = ObjectId(body.card_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Card not found")

    card = await cards_collection.find_one(
        {"_id": card_oid, "user_id": user_id, "deleted_at": None}
    )
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Call Groq to evaluate the answer
    try:
        client = _get_groq_client()
    except RuntimeError:
        logger.error("[quiz] GROQ_API_KEY is not configured")
        raise HTTPException(status_code=500, detail="AI service is not configured. Contact support.")

    # Use server-stored question text to prevent client-side prompt injection
    stored_question = session.get("current_question", {})
    server_question_text: str = stored_question.get("question_text") or body.question_text
    server_question_type: str = stored_question.get("question_type") or body.question_type

    # Skip path — bypass LLM, mark incorrect, reveal answer, advance immediately.
    if body.skip:
        eval_result = {
            "response_type": "evaluation",
            "evaluation": "incorrect",
            "feedback_message": "",
            "hint": None,
            "revealed_answer": card.get("back", ""),
        }
    else:
        eval_result = await _evaluate_answer(
            card=card,
            question_text=server_question_text,
            question_type=server_question_type,
            user_answer=body.user_answer,
            attempt_number=body.attempt_number,
            client=client,
            conversation_history=body.conversation_history,
        )

    response_type: str = eval_result["response_type"]

    # If this is a follow-up (question/explanation request), return immediately —
    # do not score it, do not advance the card index.
    if response_type == "followup":
        return SubmitAnswerResponse(
            response_type="followup",
            evaluation=None,
            feedback_message=eval_result["feedback_message"],
            revealed_answer=None,
            hint=None,
            score_delta=None,
            next_question=None,
            session_complete=False,
            summary=None,
        )

    evaluation: str = eval_result["evaluation"]
    score_delta: int = 1 if evaluation == "correct" else (-1 if evaluation == "incorrect" else 0)

    # Mirror the AI quiz retry logic: only advance when the answer is correct or
    # partial, OR the user has exhausted their second attempt. On "incorrect" at
    # attempt 1, keep the same card so the student gets one hint-assisted retry.
    should_advance: bool = evaluation in ("correct", "partial") or body.attempt_number >= 2

    # Update session in MongoDB only for actual answer evaluations
    card_fields: dict = _extract_card_fields(card)
    result_entry: dict = {
        "card_id": body.card_id,
        "card_title": card_fields.get("term") or card_fields.get("definition", "")[:60],
        "evaluation": evaluation,
        "score_delta": score_delta,
    }
    current_index: int = session.get("current_index", 0)
    new_index: int = current_index + (1 if should_advance else 0)
    card_ids: list[str] = session.get("card_ids", [])
    is_complete: bool = should_advance and (new_index >= len(card_ids))

    if should_advance:
        update_doc: dict = {
            "$push": {"results": result_entry},
            "$set": {"current_index": new_index},
        }
        if is_complete:
            update_doc["$set"]["status"] = "complete"

        await quiz_sessions_collection.update_one(
            {"session_id": body.session_id},
            update_doc,
        )

    # Build next question or summary based on advance decision
    next_question: QuizQuestion | None = None
    summary: QuizSummary | None = None

    if not should_advance:
        # Retry scenario — the frontend keeps the current question active.
        # next_question and summary both remain None.
        pass
    elif not is_complete:
        next_card_id: str = card_ids[new_index]
        try:
            next_card_oid = ObjectId(next_card_id)
        except Exception:
            raise HTTPException(status_code=500, detail="Corrupted session card reference")

        next_card = await cards_collection.find_one(
            {"_id": next_card_oid, "deleted_at": None}
        )
        if next_card:
            next_question = await _build_question(
                card=next_card,
                card_index=new_index,
                total_deck_cards=len(card_ids),
                client=client,
            )
            # Persist new question server-side so next /answer call uses the real text
            await quiz_sessions_collection.update_one(
                {"session_id": body.session_id},
                {"$set": {"current_question": {
                    "card_id": next_question.card_id,
                    "question_type": next_question.question_type,
                    "question_text": next_question.question_text,
                }}},
            )
    else:
        # Compute summary — collect all results from updated session
        all_results: list[dict] = session.get("results", []) + [result_entry]
        weak_card_ids: list[str] = [
            r["card_id"] for r in all_results if r.get("evaluation") == "incorrect"
        ]

        # Fetch terms for weak cards
        weak_card_terms: list[str] = []
        for wc_id in weak_card_ids:
            try:
                wc_doc = await cards_collection.find_one(
                    {"_id": ObjectId(wc_id)},
                    {"front": 1, "fields": 1},
                )
                if wc_doc:
                    fields = wc_doc.get("fields", {})
                    if fields:
                        term = next(iter(fields.values()), "")
                    else:
                        term = wc_doc.get("front", "")
                    weak_card_terms.append(str(term)[:100])
            except Exception:
                weak_card_terms.append("")

        summary = _compute_summary(all_results, weak_card_terms, source="deck_quiz")
        logger.info(
            f"[quiz] Session complete: session={body.session_id}, "
            f"score={summary.score_percentage}%, user={user_id}"
        )

        # Persist permanent session history.
        # Deck cards are referenced by card_id — the card document is the source
        # of truth for term/definition, so we store only a lean reference here.
        deck_session_cards: list[dict] = [
            {
                "card_id": r["card_id"],
                "card_title": r.get("card_title"),
                "evaluation": r.get("evaluation", "incorrect"),
            }
            for r in all_results
        ]
        # Fetch deck name for a human-readable history label (non-fatal)
        deck_name_str: str | None = None
        try:
            deck_doc = await decks_collection.find_one(
                {"_id": ObjectId(session["deck_id"])},
                {"name": 1},
            )
            if deck_doc:
                deck_name_str = deck_doc.get("name")
        except Exception:
            pass
        await _write_study_session(
            user_id=user_id,
            session_type="deck_quiz",
            started_at=session.get("created_at", datetime.now(timezone.utc).replace(tzinfo=None)),
            total_cards=len(all_results),
            correct=summary.correct,
            partial=summary.partial,
            incorrect=summary.incorrect,
            score_percentage=summary.score_percentage,
            cards=deck_session_cards,
            deck_id=session.get("deck_id"),
            deck_name=deck_name_str,
        )

    return SubmitAnswerResponse(
        response_type="evaluation",
        evaluation=evaluation,  # type: ignore[arg-type]
        feedback_message=eval_result["feedback_message"],
        revealed_answer=eval_result.get("revealed_answer"),
        hint=eval_result.get("hint"),
        score_delta=score_delta,  # type: ignore[arg-type]
        next_question=next_question,
        session_complete=is_complete,
        summary=summary,
    )
