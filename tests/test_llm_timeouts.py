"""QA-005: no LLM call can hang a request."""
import os
import re
import socket
import threading
import time

import pytest

from app.core import llm_timeouts

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def _app_sources():
    for root, _, files in os.walk(APP_DIR):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    yield os.path.relpath(path, APP_DIR), handle.read()


def test_the_bounded_client_carries_the_timeout_and_one_retry():
    client = llm_timeouts.bounded_groq("test-key")
    assert client.timeout == llm_timeouts.LLM_TIMEOUT_SECONDS
    assert client.max_retries == 1


def test_a_malformed_setting_keeps_the_default(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "soon")
    assert llm_timeouts._seconds("LLM_TIMEOUT_SECONDS", 60.0) == 60.0
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0")
    assert llm_timeouts._seconds("LLM_TIMEOUT_SECONDS", 60.0) == 1.0


def test_no_groq_client_is_built_without_the_bound():
    offenders = [path for path, source in _app_sources() if re.search(r"\bGroq\(", source) and path != "core/llm_timeouts.py"]
    assert offenders == []


def test_every_gemini_call_passes_the_request_options():
    calls = []
    for path, source in _app_sources():
        for match in re.finditer(r"\.(generate_content|send_message)\(", source):
            depth, end = 1, match.end()
            while depth and end < len(source):
                depth += {"(": 1, ")": -1}.get(source[end], 0)
                end += 1
            calls.append((path, "request_options=GEMINI_REQUEST_OPTIONS" in source[match.end():end]))
    assert calls, "the scan found no Gemini calls; the pattern is stale"
    assert [path for path, bounded in calls if not bounded] == []


@pytest.fixture
def silent_server():
    """Accepts connections and never answers: a provider that has hung."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held = []
    stop = threading.Event()

    def accept():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join()
    for connection in held:
        connection.close()
    server.close()


def test_a_hung_provider_costs_the_timeout_not_forever(monkeypatch, silent_server):
    import groq

    monkeypatch.setenv("GROQ_BASE_URL", silent_server)
    monkeypatch.setattr(llm_timeouts, "LLM_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(llm_timeouts, "LLM_MAX_RETRIES", 0)
    client = llm_timeouts.bounded_groq("test-key")

    started = time.monotonic()
    with pytest.raises(groq.APITimeoutError):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert time.monotonic() - started < 3
