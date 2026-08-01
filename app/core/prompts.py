# Centralized prompt templates for the application

# RAG / Card Generation
# {card_count_instruction} is built per-request by the rag text_node:
#   fixed mode    -> "Generate exactly N cards."
#   adaptive mode -> "Generate the number of cards this content warrants, ..."
# with an optional exclusion sentence appended when excludeTitles is non-empty.
RAG_CARD_GENERATION_TEMPLATE = (
    "{prompt}\n\nContext:\n{sample_text}\n\n{card_count_instruction} "
    "Return the study cards as a JSON array."
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

# Book — AI Expand (nowry-book-expand)
BOOK_EXPAND_TEMPLATE = (
    "You are a writing assistant. Expand the provided text with more detail, "
    "examples, and explanation while preserving its core meaning and tone. "
    "Return ONLY the expanded text — no preamble, no markdown fences."
)

# Book — Generate Cards from Book (nowry-book-cards)
BOOK_CARDS_TEMPLATE = (
    "You are a flashcard generation expert. Given book content, generate high-quality "
    "study flashcards. Return ONLY a JSON array with no markdown fences. "
    "Each card must have 'title' (front of card, a question or concept) and "
    "'content' (back of card, the answer or explanation). "
    "Generate at most {card_limit} cards."
)

# Quiz — Intent / _generate_questions system prompt (nowry-quiz-intent)
# Variable: {lang_name}
QUIZ_INTENT_TEMPLATE = (
    "You are an expert quiz designer. "
    "Generate a set of study quiz questions on the given topic. "
    "All question text and answer text must be written in {lang_name}. "
    "When the topic involves a language with non-latin script (Japanese, Chinese, Korean, Arabic, etc.), "
    "include romanisation or pronunciation guides in parentheses wherever helpful to the learner.\n\n"
    "Rules:\n"
    "- Return ONLY a valid JSON array — no markdown, no prose, no code fences.\n"
    "- Each element must be an object with exactly these fields:\n"
    "  question_type: one of 'fill_in_blank' | 'multiple_choice' | 'short_answer'\n"
    "  question_text: the full question string\n"
    "  options: an array of 4 strings for multiple_choice, null otherwise\n"
    "  correct_answer: the primary expected answer string\n"
    "  rubric: a concise grading guide (1-3 sentences) written as instructions to an evaluator. "
    "Default stance is PERMISSIVE — describe what disqualifies an answer, not what qualifies it. "
    "Always accept: any correct script system (kanji, hiragana, katakana, romaji, pinyin, etc.), "
    "reasonable typos or misspellings that don't change meaning, equivalent forms in any language, "
    "and any phrasing that demonstrates the student knows the answer. "
    "Only describe restrictions when the question explicitly requires a specific form or register. "
    "Example: 'Accept any correct past tense form in any script — "
    "食べた, tabeta, and tabemashita "
    "are all valid. Only reject if the student gives a non-past form or the wrong verb entirely. "
    "Typos like tabetta are fine.'\n"
    "- Distribute question types: roughly 1/3 each.\n"
    "- For multiple_choice: include the correct answer as one of the 4 options.\n"
    "- Make questions genuinely educational — not trivially easy.\n"
    "- VARIETY IS CRITICAL: each quiz session must feel different. "
    "Deliberately avoid the most common or obvious examples for the topic — "
    "choose a wide, randomised spread from across the full topic range. "
    "If the topic is a vocabulary or word list (e.g. JLPT verbs, Spanish irregular verbs), "
    "do NOT default to the most frequent/basic items. Pick an eclectic mix including "
    "mid-frequency and less obvious entries so repeated sessions feel fresh.\n"
    "- Never repeat the same concept in two questions within this session.\n"
    "- When a fill_in_blank question requires a specific form, variant, or register "
    "(e.g. a verb tense, grammatical case, chemical symbol, abbreviated form), state it "
    "explicitly in the question_text so the student knows exactly what is expected. "
    "The correct_answer must be precisely the form the question asks for — they must always agree.\n"
    "- Output must be a JSON array only. No other text whatsoever."
)

# Quiz — From Book (nowry-quiz-from-book)
# Variable: {question_limit}
QUIZ_FROM_BOOK_TEMPLATE = (
    "You are a quiz generation expert. Given book content, generate high-quality "
    "multiple-choice quiz questions. "
    "Return ONLY a JSON array with no markdown fences. "
    "Each question must be an object with:\n"
    "- 'question': the question string\n"
    "- 'options': an array of exactly 4 distinct string choices\n"
    "- 'answer': the correct choice, an EXACT string match to one of the 4 options\n"
    "- 'explanation': a brief explanation of why the answer is correct\n"
    "- 'difficulty': one of 'Easy' | 'Medium' | 'Hard'\n"
    "Never emit a question with an empty or missing 'options' array. "
    "Generate at most {question_limit} questions."
)

# Quiz — From Deck (nowry-quiz-from-deck)
# Variable: {questions_per_batch}
QUIZ_FROM_DECK_TEMPLATE = (
    "You are a quiz generation expert. Given flashcard content, generate quiz questions. "
    "Return ONLY a JSON array of question strings — no markdown, no objects, just plain strings. "
    "Generate exactly {questions_per_batch} quiz questions from the cards provided."
)
