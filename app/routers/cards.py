from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pymongo.collection import Collection
from bson import ObjectId
from app.models.StudyCard import StudyCard
from app.models.CardGenerationRequest import CardGenerationRequest
from app.config.database import cards_collection
from app.ai_client.groq_client import Groq_client
import json

from ..routers.book_pages import save_book_page

router = APIRouter(
    prefix="/card",
    tags=["cards"],
    responses={404: {"description": "Not found"}},
)


# Define a dependency to access the books collection from MongoDB
def get_cards_collection() -> Collection:
    return cards_collection


@router.post("/generate", summary="Generate a new card using AI")
async def generate_card(payload: CardGenerationRequest):
    try:
        prompt = payload.prompt
        sampletText = payload.sampleText
        sampleNumber = payload.sampleNumber

        if not prompt or not sampletText or sampleNumber is None:
            raise HTTPException(status_code=400, detail="Invalid input data")

        # Integrate the AI generation logic here
        groq_client = Groq_client()
        request_string = f"{prompt} {sampletText}. create {sampleNumber} samples."

        ai_response = groq_client.request(request_string)

        # Assuming the AI returns a response with card details
        response_content = ai_response.choices[0].message.content
        print(f"reponse_content", response_content)
        study_cards = response_content[
            response_content.find("[") : response_content.rfind("]") + 1
        ]

        # Return the generated card without saving it to the database
        print(f"returning", study_cards)
        return json.loads(study_cards)
    except json.JSONDecodeError as e:
        print(f"Error parsing the list: {e}")
        return []
    except Exception as ex:
        print(f"There was an error while generating the cards: {ex}")


@router.post("/create", summary="Create a new card", response_model=StudyCard)
async def create_card(
    card: StudyCard, cards_collection: Collection = Depends(get_cards_collection)
):
    import pdb

    pdb.set_trace()
    return card
