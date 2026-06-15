"""
visualizer_node.py — refactored for tier-based LLM client injection (D-02).

The LLM client is injected into state by AIOrchestrator.invoke() based on
the user's subscription tier. This node no longer manages client lifecycle.
"""
import json
from fastapi import HTTPException
from pydantic import BaseModel, Field
from app.core import prompt_manager
from app.core.evaluation_helper import score_trace


class VisualizerOutput(BaseModel):
    mermaid_code: str = Field(description="The valid mermaid.js code")
    explanation: str = Field(description="Brief explanation of the diagram")


def generate_visual_node(state):
    text = state["text"]
    viz_type = state.get("viz_type", "mindmap")

    # Read llm_client from orchestrator-injected state (D-02)
    llm_client = state.get("llm_client")
    if not llm_client:
        raise HTTPException(status_code=500, detail="LLM client not injected into state")

    # Build prompt string via centralized prompt manager (D-13)
    format_instructions = (
        "Return a JSON object with exactly these keys: "
        "mermaid_code (string: the valid mermaid.js diagram code), "
        "explanation (string: brief explanation of the diagram). "
        "Do not wrap in markdown code blocks."
    )
    prompt_string = prompt_manager.get_prompt(
        "nowry-viz-magic",
        text=text,
        viz_type=viz_type,
        format_instructions=format_instructions,
    )

    try:
        ai_response = llm_client.request(prompt_string)
        raw_text = ai_response.choices[0].message.content

        # Parse JSON response (model is instructed to return JSON)
        result = json.loads(raw_text)
        # D-04: no comment on success
        score_trace(name="format-valid", value=True)
        return {
            "mermaid_code": result.get("mermaid_code", ""),
            "explanation": result.get("explanation", ""),
        }
    except json.JSONDecodeError as e:
        # D-03: truncated error + raw-output snippet (~300 chars)
        snippet = raw_text[:300]
        score_trace(
            name="format-valid",
            value=False,
            comment=f"{e}\nRaw output (truncated): {snippet}",
        )
        # Keep existing soft-failure behavior
        return {"error": f"Failed to parse visualizer response: {e}"}
    except Exception as e:
        print(f"Error generating visual: {e}")
        return {"error": str(e)}
