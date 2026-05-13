"""
Gemini LLM client for Plus and Pro tier routing (D-02).

Exposes the same request() interface as Groq_client so node functions
(text_node, quiz_node, etc.) need no changes — they call
    ai_response = llm_client.request(request_string)
    raw_output = ai_response.choices[0].message.content
regardless of whether llm_client is Groq_client or Gemini_client.
"""
import os
from dataclasses import dataclass, field
from typing import List
import google.generativeai as genai


# ---------------------------------------------------------------------------
# Groq-compatible response shim
# ---------------------------------------------------------------------------

@dataclass
class _GeminiMessage:
    content: str


@dataclass
class _GeminiChoice:
    message: _GeminiMessage


@dataclass
class _GeminiResponseShim:
    """Wraps Gemini response.text into the Groq SDK response shape.

    Callers can use:
        response.choices[0].message.content
    which matches the Groq SDK contract expected by text_node.py.
    """
    choices: List[_GeminiChoice] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> "_GeminiResponseShim":
        return cls(choices=[_GeminiChoice(message=_GeminiMessage(content=text))])


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

class Gemini_client:
    """Gemini Generative AI client.

    Args:
        model_id: Gemini model identifier. Defaults to gemini-flash-latest (Plus tier).
                  Pass "models/gemini-pro-latest" for Pro tier.
    """

    def __init__(self, model_id: str = "models/gemini-flash-latest") -> None:
        """Initialize Gemini client. Raises ValueError if GEMINI_API_KEY is missing."""
        api_key: str = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY environment variable")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model_id)
        self._model_id = model_id

    def request(self, request_string: str) -> _GeminiResponseShim:
        """Send a completion request to Gemini.

        Returns a _GeminiResponseShim so callers can access
        .choices[0].message.content — matching the Groq SDK response shape.

        Args:
            request_string: The prompt text to send to Gemini.

        Returns:
            _GeminiResponseShim with the response text.

        Raises:
            RuntimeError: If Gemini API call fails.
        """
        try:
            response = self.model.generate_content(
                request_string,
                generation_config=genai.types.GenerationConfig(temperature=0.7),
            )
            return _GeminiResponseShim.from_text(response.text)
        except Exception as e:
            raise RuntimeError(f"Gemini API call failed: {type(e).__name__}") from e
