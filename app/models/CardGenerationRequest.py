from pydantic import BaseModel


class CardGenerationRequest(BaseModel):
    prompt: str
    sampleText: str
    sampleNumber: int
