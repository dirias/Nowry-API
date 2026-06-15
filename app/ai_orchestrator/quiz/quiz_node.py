import json
from fastapi import HTTPException
from app.core import prompt_manager
from app.core.evaluation_helper import score_trace


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

    # Prepare custom instructions
    custom_instr = ""
    if custom_prompt:
        custom_instr = f"Additional Instructions: {custom_prompt}\n\n"

    # Build system prompt via centralized prompt manager (D-13)
    system_prompt = prompt_manager.get_prompt(
        "nowry-quiz-magic",
        difficulty=difficulty,
        num_questions=num_questions,
        custom_instructions=custom_instr,
    )

    request_string = f"{system_prompt}\n\nProvided Context:\n{sample_text}"

    # Use state-injected LLM client (injected by AIOrchestrator.invoke based on tier)
    llm_client = state.get("llm_client")
    if not llm_client:
        raise HTTPException(status_code=500, detail="LLM client not injected into state")

    try:
        ai_response = llm_client.request(request_string)
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
            # D-04: no comment on success
            score_trace(name="format-valid", value=True)
            return {"generated_quiz": quiz_data}
        else:
            raise ValueError("No JSON array found in response")

    except (json.JSONDecodeError, ValueError) as e:
        # D-02 CRITICAL: record the failure score BEFORE raising -- the trace
        # context (propagate_attributes) is still active here, but NOT after
        # the HTTPException propagates out of graph.invoke().
        snippet = raw_output[:300]
        score_trace(
            name="format-valid",
            value=False,
            comment=f"{e}\nRaw output (truncated): {snippet}",
        )
        print(f"Quiz JSON Generation failed: {e}")
        print(f"Raw Output: {raw_output}")
        # Fallback or error
        raise HTTPException(
            status_code=500, detail="AI failed to generate valid JSON for quiz."
        )
    except Exception as e:
        print(f"Quiz Generation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
