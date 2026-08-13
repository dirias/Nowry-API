import json
from fastapi import HTTPException
from app.core import prompt_manager
from app.core.evaluation_helper import score_trace


def build_card_count_instruction(
    cap: int, adaptive: bool, exclude_titles: list[str]
) -> str:
    """Build the mode-dependent count instruction for nowry-cards-magic.

    Fixed mode asks for exactly `cap` cards (legacy behavior); adaptive mode
    lets the model choose within [1, cap]. When exclude_titles is non-empty,
    an exclusion sentence is appended so "Generate more" requests do not
    duplicate concepts already produced.
    """
    if adaptive:
        instruction: str = (
            "Generate the number of cards this content warrants, between 1 "
            f"and {cap}; one card per term for term–definition lists; 2–5 "
            "conceptual cards per dense paragraph."
        )
    else:
        instruction = f"Generate exactly {cap} cards."
    if exclude_titles:
        titles: str = ", ".join(exclude_titles)
        instruction += (
            " Do not create cards covering these already-generated "
            f"concepts: {titles}."
        )
    return instruction


def text_node(state):
    """
    Generates study cards from a prompt and contextual text using an LLM.
    """

    prompt = state.get("prompt")
    sample_text = state.get("sampleText")
    sample_number = state.get("sampleNumber")

    if not prompt or not sample_text or sample_number is None:
        raise HTTPException(status_code=400, detail="Invalid input data")

    adaptive: bool = bool(state.get("adaptive"))
    exclude_titles: list[str] = state.get("excludeTitles") or []
    card_count_instruction: str = build_card_count_instruction(
        sample_number, adaptive, exclude_titles
    )

    # Construct LLM request via centralized prompt manager (D-13).
    # sample_number is still passed so a stale Langfuse-hosted template that
    # references {{sample_number}} keeps compiling (extra vars are ignored by
    # templates that do not use them).
    request_string = prompt_manager.get_prompt(
        "nowry-cards-magic",
        prompt=prompt,
        sample_text=sample_text,
        sample_number=sample_number,
        card_count_instruction=card_count_instruction,
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
        # parse_error lets the streaming route distinguish "LLM returned
        # malformed JSON" from "LLM validly returned zero cards". The sync
        # route ignores this key — its behavior is unchanged.
        return {"generated_cards": [], "parse_error": True}
