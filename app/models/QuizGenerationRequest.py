from pydantic import BaseModel
from typing import Optional


class QuizGenerationRequest(BaseModel):
    sampleText: str
    numQuestions: int = 5
    prompt: Optional[str] = None
    difficulty: str = "Medium"  # Easy, Medium, Hard
