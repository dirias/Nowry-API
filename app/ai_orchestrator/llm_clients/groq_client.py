import os

from app.core.llm_timeouts import bounded_groq


class Groq_client:
    def __init__(self) -> None:
        """Initialize Groq client."""
        api_key = os.getenv("GROQ_API_KEY")
        groq_model = os.getenv("GROQ_MODEL")

        if not api_key:
            raise ValueError("Missing GROQ_API_KEY environment variable")

        self.client = bounded_groq(api_key)
        self.model = groq_model

    def request(self, request_string: str) -> dict:
        """Send a chat completion request."""
        chat_completion = self.client.chat.completions.create(
            messages=[{"role": "user", "content": request_string}],
            model=self.model,
        )
        return chat_completion
