from app.ai_orchestrator.llm_clients.groq_client import Groq_client
import json
from fastapi import HTTPException


def quiz_node(state):
    """
    Generates a multiple-choice quiz from the provided text.
    """
    sample_text = state.get("sampleText")
    num_questions = state.get("numQuestions", 5)
    difficulty = state.get("difficulty", "Medium")
    custom_prompt = state.get("prompt")

    if not sample_text:
        raise HTTPException(
            status_code=400, detail="No text provided for quiz generation"
        )

    # Default robust prompt
    system_prompt = (
        f"You are an expert teacher. Create a {difficulty} level multiple-choice quiz "
        f"with {num_questions} questions based PROPERLY and STRICTLY on the Provided Context below.\n"
        "Return the result as a raw JSON array of objects. Do not wrap in markdown code blocks.\n"
        "Each object must have:\n"
        "- 'question': The question string\n"
        "- 'options': An array of 4 distinct string choices\n"
        "- 'answer': The correct option string (exact match to one of the options)\n"
        "- 'explanation': A brief explanation of why the answer is correct\n\n"
    )

    if custom_prompt:
        system_prompt += f"Additional Instructions: {custom_prompt}\n\n"

    request_string = f"{system_prompt}\n\nProvided Context:\n{sample_text}"

    groq_client = Groq_client()
    # Assuming the client handles the call. We might need to adjust if Groq_client API differs.
    # Based on text_node.py it uses .request(prompt)

    try:
        ai_response = groq_client.request(request_string)
        # Extract content
        raw_output = ai_response.choices[0].message.content.strip()

        # Clean markdown if present
        if raw_output.startswith("```json"):
            raw_output = raw_output[7:]
        if raw_output.endswith("```"):
            raw_output = raw_output[:-3]

        # Locate JSON array bounds just in case of chatter
        start_idx = raw_output.find("[")
        end_idx = raw_output.rfind("]")

        if start_idx != -1 and end_idx != -1:
            json_str = raw_output[start_idx : end_idx + 1]
            quiz_data = json.loads(json_str)
            return {"generated_quiz": quiz_data}
        else:
            raise ValueError("No JSON array found in response")

    except json.JSONDecodeError as e:
        print(f"Quiz JSON Generation failed: {e}")
        print(f"Raw Output: {raw_output}")
        # Fallback or error
        raise HTTPException(
            status_code=500, detail="AI failed to generate valid JSON for quiz."
        )
    except Exception as e:
        print(f"Quiz Generation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
