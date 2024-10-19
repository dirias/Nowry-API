from typing import List
from pydantic import BaseModel
from typing import List, Optional
from models import StudyCard


class Deck(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    cards: List[StudyCard] = []

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "id": 1,
                "name": "Physics Deck",
                "description": "Deck containing cards for various topics in physics.",
                "cards": [],
            }
        }
