import os
from groq import Groq


class Groq_client:

    def __init__(self) -> None:
        """ """
        self.client = Groq(
            api_key="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",
        )
        self.model = "llama3-8b-8192"

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
