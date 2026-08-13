from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class DiagramRequest(BaseModel):
    selected_text: str
    diagram_type: Literal["auto", "mindmap", "flowchart", "sequence", "er"] = "auto"


class DiagramResponse(BaseModel):
    mermaid_code: str
    explanation: str
