"""QA-006: every secure route refuses a request without a valid token.

Walks every operation in the app's OpenAPI schema rather than testing routers
one by one, so a route added tomorrow without the auth dependency fails here
the day it is added. The public ones are named below; adding to that list is a
decision, not a fix for a red test.
"""
import inspect
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

#: Operations that answer without a token, each on purpose.
PUBLIC = {
    "GET /": "liveness",
    "GET /health": "readiness for the uptime monitor (QA-004)",
    "GET /beta/config": "the clients read the beta flags before sign-in (ADR-038)",
    "POST /beta/invites/check": "checked before the Firebase account exists (ADR-038)",
    "POST /beta/waitlist": "for people without an account (ADR-038)",
    "POST /contact": "the public contact form",
    "POST /copyright-notices": "takedown notices come from rights holders (GTM-008)",
    "POST /stripe/webhook": "authenticated by Stripe's signature, not a user token",
    "POST /session/login": "deprecated, answers 410",
    "POST /session/logout": "deprecated, answers 410",
    "POST /session/reset_password": "deprecated, answers 410",
    "GET /public/books": "the public library, browsable signed out",
    "GET /public/decks": "the public library, browsable signed out",
    "GET /public/books/{book_id}": "a published book, readable signed out",
    "GET /public/decks/{deck_id}": "a published deck, readable signed out",
    "GET /public/decks/{deck_id}/cards": "a published deck's cards, readable signed out",
}

OBJECT_ID = "507f1f77bcf86cd799439011"
REFUSED = {401, 403}


@pytest.fixture(scope="module")
def app():
    from app.main import app as application
    import app.auth.firebase_auth as firebase_auth

    # Several test modules stub sys.modules["app.auth.firebase_auth"]; a sweep
    # over a mocked dependency would prove nothing.
    assert inspect.iscoroutinefunction(firebase_auth.get_firebase_user), "auth module is stubbed"
    limiter = application.state.limiter
    was_enabled = getattr(limiter, "enabled", True)
    limiter.enabled = False  # 200-odd requests from one address in a second
    yield application
    limiter.enabled = was_enabled


def _operations(app) -> list:
    operations = []
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            operations.append((method.upper(), path))
    return operations


def _call(client: TestClient, method: str, path: str, headers=None):
    url = re.sub(r"\{[^}]+\}", OBJECT_ID, path)
    return client.request(method, url, json={}, headers=headers or {})


def test_the_sweep_sees_the_whole_api(app):
    # A guard on the guard: if routing changes how routes are listed, an empty
    # walk would pass every assertion below.
    assert len(_operations(app)) > 200


def test_every_public_entry_still_exists(app):
    listed = {f"{method} {path}" for method, path in _operations(app)}
    assert sorted(set(PUBLIC) - listed) == []


def test_no_token_is_refused_everywhere_else(app):
    client = TestClient(app, raise_server_exceptions=False)
    open_doors = []
    for method, path in _operations(app):
        if f"{method} {path}" in PUBLIC:
            continue
        status = _call(client, method, path).status_code
        if status not in REFUSED:
            open_doors.append(f"{method} {path} -> {status}")
    assert open_doors == []


def test_an_invalid_token_is_refused_everywhere_else_and_says_nothing_of_why(app):
    client = TestClient(app, raise_server_exceptions=False)
    secret = "kid mismatch in project nowry-88f05"
    open_doors, leaks = [], []
    with patch("app.auth.firebase_auth.verify_firebase_token", side_effect=ValueError(secret)):
        for method, path in _operations(app):
            if f"{method} {path}" in PUBLIC:
                continue
            response = _call(client, method, path, {"Authorization": "Bearer forged.token.value"})
            if response.status_code not in REFUSED:
                open_doors.append(f"{method} {path} -> {response.status_code}")
            elif secret in response.text:
                leaks.append(f"{method} {path}")
    assert open_doors == []
    assert leaks == []
