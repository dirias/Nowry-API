"""
Phase 7 Agent Models — personality generation, board-to-card parsing.
"""
import json
import re
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class GeneratePersonalityRequest(BaseModel):
    style_hints: Optional[str] = Field(None, max_length=200)
    pet_species: Optional[str] = None


class GeneratePersonalityResponse(BaseModel):
    personality_text: str
    generations_used: int
    generations_limit: int
    reset_date: Optional[str] = None   # "YYYY-MM" string


class GeneratedCardPair(BaseModel):
    front: str = Field(..., min_length=3, max_length=500)
    back: str = Field(..., min_length=3, max_length=500)

    @field_validator("front", "back")
    @classmethod
    def strip_and_validate(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Card field cannot be empty after stripping")
        return cleaned


class BoardToCardOutput(BaseModel):
    cards: List[GeneratedCardPair]


def _parse_card_output(raw_text: str) -> BoardToCardOutput:
    """
    Parse LLM text into BoardToCardOutput.
    Attempt 1: JSON array (from markdown code block or raw text).
    Attempt 2: Q:/A: or front:/back: line format.
    Raises ValueError if neither succeeds.
    """
    # Attempt 1: JSON array
    json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', raw_text, re.DOTALL)
    if not json_match:
        json_match = re.search(r'(\[.*?\])', raw_text, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            cards = []
            for item in parsed:
                # Support both "front"/"back" and "question"/"answer" keys
                front = item.get("front") or item.get("question", "")
                back = item.get("back") or item.get("answer", "")
                if front and back:
                    cards.append(GeneratedCardPair(front=front, back=back))
            if cards:
                return BoardToCardOutput(cards=cards)
        except Exception:
            pass  # fall through

    # Attempt 2: Q:/A: line format
    cards: List[GeneratedCardPair] = []
    front = back = None
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if line.lower().startswith(("q:", "front:")):
            front = line.split(":", 1)[1].strip()
        elif line.lower().startswith(("a:", "back:")) and front:
            back = line.split(":", 1)[1].strip()
            try:
                cards.append(GeneratedCardPair(front=front, back=back))
            except Exception:
                pass
            front = back = None

    if not cards:
        raise ValueError(f"No Q/A pairs parseable from LLM output: {raw_text[:200]!r}")
    return BoardToCardOutput(cards=cards)
