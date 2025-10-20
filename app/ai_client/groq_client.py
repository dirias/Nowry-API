import os
from groq import Groq


class Groq_client:

    def __init__(self) -> None:
        """ """
        self.client = Groq(
            api_key="",
        )
        self.model = "openai/gpt-oss-20b"

    def request(self, request_string) -> dict:
        """ """
        chat_completion = self.client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": request_string,
                }
            ],
            model=self.model,
        )
        return chat_completion
