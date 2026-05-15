from __future__ import annotations

from pydantic import BaseModel


class AIExpandRequest(BaseModel):
    selected_text: str
    instruction: str = "Expand this text with more detail and examples."


class AIExpandResponse(BaseModel):
    expanded_text: str
