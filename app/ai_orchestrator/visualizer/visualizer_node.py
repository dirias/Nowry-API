from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_core.output_parsers import JsonOutputParser
from pydantic import BaseModel, Field
import os


# Define output structure
class VisualizerOutput(BaseModel):
    mermaid_code: str = Field(description="The valid mermaid.js code")
    explanation: str = Field(description="Brief explanation of the diagram")


# Setup LLM
# Using llama-3.3-70b-versatile for better reasoning on structure
llm = ChatGroq(
    temperature=0.2,
    model_name="llama-3.3-70b-versatile",
    groq_api_key=os.getenv("GROQ_API_KEY"),
)

parser = JsonOutputParser(pydantic_object=VisualizerOutput)


def generate_visual_node(state):
    text = state["text"]
    viz_type = state.get("viz_type", "mindmap")  # mindmap, flow, sequence, etc.

    prompt_template = """
    You are an expert in Data Visualization and Mermaid.js.
    Your task is to create a valid Mermaid.js diagram code based on the provided text.

    Type requested: {viz_type}

    Rules:
    1. Output MUST be valid Mermaid.js syntax.
    2. Do NOT use markdown code blocks (```mermaid). Just return the code string within the JSON.
    3. For 'mindmap': Use 'mindmap' syntax. Root node at center.
    4. For 'flowchart': Use 'graph TD' or 'graph LR'. Use standard arrows.
       - Valid: A --> B
       - Valid: A -->|Label| B
       - INVALID: A -->|Label|> B (Do not put > after the label pipe)
    5. For 'sequence': Use 'sequenceDiagram'.
    6. Simplify the text to fit nodes. Keep labels concise.
    7. Ensure no syntax errors (e.g. matching brackets).

    Input Text:
    {text}

    {format_instructions}
    """

    prompt = ChatPromptTemplate.from_template(template=prompt_template)

    chain = prompt | llm | parser

    try:
        result = chain.invoke(
            {
                "text": text,
                "viz_type": viz_type,
                "format_instructions": parser.get_format_instructions(),
            }
        )
        return {
            "mermaid_code": result["mermaid_code"],
            "explanation": result["explanation"],
        }
    except Exception as e:
        print(f"Error generating visual: {e}")
        # Fallback error
        return {"error": str(e)}
