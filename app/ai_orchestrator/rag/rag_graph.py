from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional, List, Any
from .text_node import text_node


class RAGState(TypedDict):
    prompt: str
    sampleText: str
    # Effective card cap (route computes it: fixed count, or adaptive-derived).
    sampleNumber: int
    # True when the request omitted sampleNumber (adaptive card count).
    adaptive: bool
    # Titles of already-generated cards the LLM must not duplicate.
    excludeTitles: List[str]
    tier: str
    llm_client: Any
    generated_cards: Optional[List[dict]]
    # Set (True) by text_node ONLY on the JSON parse failure path.
    # The success path never sets it.
    parse_error: Optional[bool]


graph = StateGraph(RAGState)

graph.add_node("generate_cards", text_node)

graph.set_entry_point("generate_cards")
graph.add_edge("generate_cards", END)

rag_app = graph.compile()
