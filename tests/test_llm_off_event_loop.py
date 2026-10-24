"""QA-011: a slow LLM call does not stop the API for everyone else.

The Groq and Gemini SDKs used here are synchronous. Called straight from an
``async def`` handler, one call holds the worker's event loop for its whole
duration, and every other request on that worker waits behind it.
"""
import ast
import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

#: Synchronous provider calls (LLM and Google TTS), and the sync graph that makes them.
BLOCKING_CALLS = (
    "chat.completions.create",
    "generate_content",
    "send_message",
    "llm_client.request",
    "orchestrator.invoke",
    "synthesize_speech",
)


def _blocking_calls_on_the_loop() -> list:
    found = []
    for root, _, files in os.walk(APP_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source)
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = ast.unparse(node.func)
                if not func.endswith(BLOCKING_CALLS) or isinstance(parents.get(node), ast.Await):
                    continue
                scope = node
                while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    scope = parents[scope]
                if isinstance(scope, ast.AsyncFunctionDef):
                    found.append(f"{os.path.relpath(path, APP_DIR)}:{node.lineno} {func}")
    return found


def test_no_async_handler_calls_a_provider_on_the_event_loop():
    assert _blocking_calls_on_the_loop() == []


@pytest.mark.asyncio
async def test_other_requests_run_while_a_generation_waits():
    from app.routers.quiz import _generate_question_text

    def slow_create(**_kwargs):
        time.sleep(0.6)  # the provider thinking, synchronously, as the SDK does
        message = SimpleNamespace(content="What does 精々 mean?")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = MagicMock()
    client.chat.completions.create = slow_create

    ticks = []
    started = time.monotonic()

    async def another_users_request():
        await asyncio.sleep(0.05)
        ticks.append(time.monotonic() - started)

    generation, _ = await asyncio.gather(
        _generate_question_text({"front": "精々"}, "definition", client),
        another_users_request(),
    )

    assert ticks[0] < 0.3, f"the loop was held for {ticks[0]:.2f}s"
    assert generation == "What does 精々 mean?"
