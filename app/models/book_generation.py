from __future__ import annotations

from pydantic import BaseModel


class GenerateFromBookRequest(BaseModel):
    book_id: str


class GeneratedCard(BaseModel):
    title: str
    content: str


class GenerateFromBookResponse(BaseModel):
    cards: list[GeneratedCard]


class GenerateQuizFromBookRequest(BaseModel):
    book_id: str


class GenerateQuizFromBookResponse(BaseModel):
    questions: list[dict]
