from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class GenerateFromBookRequest(BaseModel):
    book_id: str


class GeneratedCard(BaseModel):
    title: str
    content: str


class GenerateFromBookResponse(BaseModel):
    cards: list[GeneratedCard]


class GenerateQuizFromBookRequest(BaseModel):
    book_id: str


class GeneratedQuizQuestion(BaseModel):
    """Canonical multiple-choice question shape consumed by the frontend.

    This mirrors the shape returned by ``POST /quiz/generate`` so that both
    quiz producers feed the same ``QuestionnaireModal`` component:
    ``question`` / ``options`` / ``answer`` / ``explanation``.

    The LLM is free to answer in a looser shape (``correct_answer`` +
    ``incorrect_answers``); the router normalises into this model before
    serialising, so the wire contract never depends on prompt wording.
    """

    question: str
    options: list[str] = Field(min_length=2)
    answer: str
    explanation: Optional[str] = None
    difficulty: Optional[str] = None


class GenerateQuizFromBookResponse(BaseModel):
    questions: list[GeneratedQuizQuestion]
