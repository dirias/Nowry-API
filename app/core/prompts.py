# Centralized prompt templates for the application

# RAG / Card Generation
RAG_CARD_GENERATION_TEMPLATE = (
    "{prompt}\n\nContext:\n{sample_text}\n\nCreate UP TO {sample_number} study card samples as JSON."
)

# Quiz Generation
QUIZ_GENERATION_TEMPLATE = (
    "You are an expert teacher. Create a {difficulty} level multiple-choice quiz "
    "with {num_questions} questions based PROPERLY and STRICTLY on the Provided Context below.\n"
    "Return the result as a raw JSON array of objects. Do not wrap in markdown code blocks.\n"
    "Each object must have:\n"
    "- 'question': The question string\n"
    "- 'options': An array of 4 distinct string choices\n"
    "- 'answer': The correct option string (exact match to one of the options)\n"
    "- 'explanation': A brief explanation of why the answer is correct\n\n"
    "{custom_instructions}"
)

# Visualizer / Mermaid Generation
VISUALIZER_GENERATION_TEMPLATE = """
    You are an expert in Data Visualization and Mermaid.js.
    Your task is to create a valid Mermaid.js diagram code based on the provided text.

    Type requested: {viz_type}

    CRITICAL RULES:
    1. Output MUST be valid Mermaid.js syntax that will parse without errors.
    2. Do NOT use markdown code blocks (```mermaid). Just return the code string within the JSON.
    
    FOR MINDMAP (CRITICAL):
    3. Syntax starts with: mindmap
    4. There MUST be EXACTLY ONE root node directly under 'mindmap'.
       - INCORRECT:
         mindmap
           Root1
           Root2
       - CORRECT:
         mindmap
           Root
             Child1
             Child2
    5. USE 2 SPACES for indentation levels. Consistency is key.
    6. Ensure every child node has a parent (proper indentation).
    7. Do not leave empty lines between nodes if it breaks the tree.

    FOR OTHER TYPES:
    8. For 'flowchart': Use 'graph TD' or 'graph LR'. Use standard arrows (-->).
       - Valid: A --> B, A["Label"] --> B
       - Avoid brackets () inside labels unless escaped.
    9. For 'sequence': Use 'sequenceDiagram'.
    
    GENERAL FORMATTING:
    10. Simplify text to fit nodes. Keep node text SHORT (max 5-7 words).
    11. Use double quotes for labels with spaces or symbols: id["Label Text"]
    12. If text is long, summarize it into key concepts.

    Input Text:
    {text}

    {format_instructions}
    """
