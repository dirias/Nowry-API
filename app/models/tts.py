from __future__ import annotations

from pydantic import BaseModel


class TTSRequest(BaseModel):
    text: str
    language_code: str = "en-US"
