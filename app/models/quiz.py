"""
Pydantic v2 models for the Active Study Partner quiz feature.

Covers both deck-based quiz sessions and AI-generated (topic-based) quiz sessions.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# GET /v1/assistant/quiz/decks
# ---------------------------------------------------------------------------


class QuizDeckInfo(BaseModel):
    deck_id: str
    name: str
    field_names: list[str]
    due_count: int
    new_count: int


class QuizDecksResponse(BaseModel):
    decks: list[QuizDeckInfo]


# ---------------------------------------------------------------------------
# POST /v1/assistant/quiz/start  (deck-based)
# ---------------------------------------------------------------------------


class StartQuizRequest(BaseModel):
    deck_id: str
    card_count: int = Field(default=10, ge=1, le=30)
    prioritize_due: bool = True


class QuizQuestion(BaseModel):
    card_id: str
    question_type: Literal["definition", "contextual", "comparison", "fill_in", "open_recall"]
    question_text: str
    card_index: int
    comparison_card_id: str | None = None


class StartQuizResponse(BaseModel):
    session_id: str
    total_cards: int
    first_question: QuizQuestion


# ---------------------------------------------------------------------------
# POST /v1/assistant/quiz/answer  (shared by deck and AI quiz)
# ---------------------------------------------------------------------------


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class SubmitAnswerRequest(BaseModel):
    session_id: str
    card_id: str
    # Accepts both deck quiz types and AI quiz types — the /answer handler
    # dispatches to the correct evaluator based on session_type in MongoDB.
    question_type: Literal[
        "definition", "contextual", "comparison", "fill_in", "open_recall",
        "fill_in_blank", "multiple_choice", "short_answer"
    ]
    question_text: str = Field(max_length=2000)  # client echoes AI-generated text; server uses stored value
    user_answer: str = Field(max_length=2000)
    attempt_number: int = Field(ge=1, le=10)
    conversation_history: Annotated[list[ConversationMessage], Field(max_length=20)] = Field(
        default_factory=list
    )
    # When True the LLM evaluator is bypassed — question is marked incorrect and the
    # session advances immediately. Used by the "Skip this card" action.
    skip: bool = False


class QuizSummary(BaseModel):
    total_cards: int
    correct: int
    partial: int
    incorrect: int
    score_percentage: int
    weak_card_ids: list[str]
    weak_card_terms: list[str]
    # Populated only when an AI quiz session auto-saves a deck on completion.
    saved_deck_id: str | None = None
    # Identifies the quiz path so the frontend can show path-specific UI
    # (e.g. "View Deck" button when source == 'ai_quiz').
    source: str | None = None


class SubmitAnswerResponse(BaseModel):
    response_type: Literal["evaluation", "followup"] = "evaluation"
    evaluation: Literal["correct", "partial", "incorrect"] | None = None
    feedback_message: str
    revealed_answer: str | None = None
    hint: str | None = None
    score_delta: Literal[1, 0, -1] | None = None
    # Union: deck quiz returns QuizQuestion; AI quiz returns AIQuizQuestionResponse.
    # Defined as the broader type to satisfy both serialisation paths.
    next_question: QuizQuestion | AIQuizQuestionResponse | None = None
    session_complete: bool = False
    summary: QuizSummary | None = None


# ---------------------------------------------------------------------------
# POST /v1/assistant/quiz/start-ai  (AI-generated topic quiz)
# ---------------------------------------------------------------------------


class AIQuizStartRequest(BaseModel):
    topic: str | None = Field(default=None, max_length=200)
    question_count: int = Field(default=10, ge=1, le=20)
    language: str = Field(default="en", max_length=10)


class AIQuizQuestionStored(BaseModel):
    """Full question document stored server-side in MongoDB — includes correct_answer and rubric."""
    card_id: str
    question_type: Literal["fill_in_blank", "multiple_choice", "short_answer"]
    question_text: str
    options: list[str] | None = None
    hint_available: bool = True
    correct_answer: str
    rubric: str = ""
    card_index: int


class AIQuizQuestionResponse(BaseModel):
    """Client-safe question shape — correct_answer is intentionally excluded."""
    card_id: str
    question_type: Literal["fill_in_blank", "multiple_choice", "short_answer"]
    question_text: str
    options: list[str] | None = None
    hint_available: bool = True
    card_index: int


class AIQuizStartResponse(BaseModel):
    session_id: str
    total_questions: int
    first_question: AIQuizQuestionResponse


# ---------------------------------------------------------------------------
# Agent chat — quiz_config extension (returned on quiz intent detection)
# ---------------------------------------------------------------------------


class QuizConfig(BaseModel):
    mode: Literal["ai", "deck"]
    topic: str | None = None
    question_count: int = 10
    deck_id: str | None = None
