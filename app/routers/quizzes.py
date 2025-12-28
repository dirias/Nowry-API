from fastapi import APIRouter, HTTPException, Depends
from app.models.QuizGenerationRequest import QuizGenerationRequest
from app.ai_orchestrator.orchestrator import orchestrator
from app.utils.logger import get_logger

router = APIRouter(
    prefix="/quiz",
    tags=["quiz"],
    responses={404: {"description": "Not found"}},
)

logger = get_logger(__name__)


@router.post("/generate", summary="Generate a quiz from text")
async def generate_quiz(payload: QuizGenerationRequest):
    try:
        logger.info(
            f"Received quiz generation request with {payload.numQuestions} questions."
        )

        # Invoke the 'quiz' graph
        result = orchestrator.invoke(
            "quiz",
            {
                "sampleText": payload.sampleText,
                "numQuestions": payload.numQuestions,
                "difficulty": payload.difficulty,
                "prompt": payload.prompt,
            },
        )

        quiz_data = result.get("generated_quiz", [])
        logger.info(f"Quiz generation completed. Items: {len(quiz_data)}")
        return quiz_data

    except HTTPException as http_err:
        raise http_err
    except Exception as ex:
        logger.exception(f"Unexpected error during quiz generation: {ex}")
        raise HTTPException(status_code=500, detail=str(ex))
