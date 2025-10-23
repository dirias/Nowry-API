from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional, List
from .text_node import text_node


class RAGState(TypedDict):
    prompt: str
    sampleText: str
    sampleNumber: int
    generated_cards: Optional[List[dict]]


graph = StateGraph(RAGState)

graph.add_node("generate_cards", text_node)

graph.set_entry_point("generate_cards")
graph.add_edge("generate_cards", END)

rag_app = graph.compile()
