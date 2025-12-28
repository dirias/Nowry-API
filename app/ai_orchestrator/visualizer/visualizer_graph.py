from typing import TypedDict, Any, Optional
from langgraph.graph import StateGraph, END
from app.ai_orchestrator.visualizer.visualizer_node import generate_visual_node


class VisualizerState(TypedDict):
    text: str
    viz_type: str
    mermaid_code: Optional[str]
    explanation: Optional[str]
    error: Optional[str]


workflow = StateGraph(VisualizerState)

# Nodes
workflow.add_node("generate_visual", generate_visual_node)

# Edges
workflow.set_entry_point("generate_visual")
workflow.add_edge("generate_visual", END)

visualizer_app = workflow.compile()
