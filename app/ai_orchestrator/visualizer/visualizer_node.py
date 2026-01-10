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

    from app.core.prompts import VISUALIZER_GENERATION_TEMPLATE

    prompt = ChatPromptTemplate.from_template(template=VISUALIZER_GENERATION_TEMPLATE)

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
