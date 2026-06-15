import json
from fastapi import HTTPException
from app.core import prompt_manager
from app.core.evaluation_helper import score_trace


def text_node(state):
    """
    Generates study cards from a prompt and contextual text using an LLM.
    """

    prompt = state.get("prompt")
    sample_text = state.get("sampleText")
    sample_number = state.get("sampleNumber")

    if not prompt or not sample_text or sample_number is None:
        raise HTTPException(status_code=400, detail="Invalid input data")

    # Construct LLM request via centralized prompt manager (D-13)
    request_string = prompt_manager.get_prompt(
        "nowry-cards-magic",
        prompt=prompt,
        sample_text=sample_text,
        sample_number=sample_number,
    )

    llm_client = state.get("llm_client")
    if not llm_client:
        raise HTTPException(status_code=500, detail="LLM client not injected into state")
    ai_response = llm_client.request(request_string)
    raw_output = ai_response.choices[0].message.content

    try:
        start_idx = raw_output.find("[")
        end_idx = raw_output.rfind("]")
        if start_idx == -1 or end_idx == -1:
            raise ValueError("No JSON array found in response")
        cards_json = raw_output[start_idx : end_idx + 1]
        study_cards = json.loads(cards_json)
        # D-04: no comment on success
        score_trace(name="format-valid", value=True)
        return {"generated_cards": study_cards}
    except (json.JSONDecodeError, ValueError) as e:
        # D-03: truncated error + raw-output snippet (~300 chars)
        snippet = raw_output[:300]
        score_trace(
            name="format-valid",
            value=False,
            comment=f"{e}\nRaw output (truncated): {snippet}",
        )
        print(f"JSON parse error: {e}")
        return {"generated_cards": []}
