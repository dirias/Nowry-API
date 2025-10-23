from app.ai_orchestrator.llm_clients.groq_client import Groq_client
import json
from fastapi import HTTPException


def text_node(state):
    """
    Generates study cards from a prompt and contextual text using an LLM.
    """

    prompt = state.get("prompt")
    sample_text = state.get("sampleText")
    sample_number = state.get("sampleNumber")

    if not prompt or not sample_text or sample_number is None:
        raise HTTPException(status_code=400, detail="Invalid input data")

    # Construct LLM request
    request_string = f"{prompt}\n\nContext:\n{sample_text}\n\nCreate {sample_number} study card samples as JSON."

    groq_client = Groq_client()
    ai_response = groq_client.request(request_string)
    raw_output = ai_response.choices[0].message.content

    try:
        cards_json = raw_output[raw_output.find("[") : raw_output.rfind("]") + 1]
        study_cards = json.loads(cards_json)
        return {"generated_cards": study_cards}
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        return {"generated_cards": []}
