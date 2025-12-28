from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional, List, Any
from .quiz_node import quiz_node


class QuizState(TypedDict):
    sampleText: str
    numQuestions: int
    difficulty: str
    prompt: Optional[str]
    generated_quiz: Optional[List[dict]]


# Define the graph
graph = StateGraph(QuizState)

# Add nodes
graph.add_node("generate_quiz", quiz_node)

# Define flow
graph.set_entry_point("generate_quiz")
graph.add_edge("generate_quiz", END)

# Compile
quiz_app = graph.compile()
