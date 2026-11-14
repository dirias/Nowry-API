"""
WAIT-002 — the waitlist in the admin area (docs/prd-admin.md FR-012, board E).

Pins:

  1. Rows come oldest first, bounded by `limit` (1..200, default 100).
  2. Filters: state (waiting, invited, joined, left), language (a row's `es-MX` is `es`;
     a code outside the five reads as English, the language its letter goes in), source.
  3. Totals per state, languages and the 20 most common sources are counted over every row.
  4. A row never carries its leave token; its language is the two-letter code.
  5. The route is admin-only and logs nothing.
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from tests._stubs import stub_if_missing

stub_if_missing("langfuse", "langfuse.langchain")
sys.modules.setdefault("app.models.agent_models", MagicMock())

import mongomock_motor
import pytest
from pydantic import ValidationError

from app.models.Beta import WaitlistAdminResponse
from app.services import admin_actions, waitlist_admin
from tests._fake_collection import FakeCollection

T0 = datetime(2026, 11, 1, 9, 0)
ADMIN = {"user_id": "65f0a1b2c3d4e5f6a7b8c9d0"}


@pytest.fixture
def rows(monkeypatch):
    collection = mongomock_motor.AsyncMongoMockClient()["nowry"]["waitlist"]
    monkeypatch.setattr(waitlist_admin, "waitlist_collection", collection)
    log = FakeCollection()
    monkeypatch.setattr(admin_actions, "admin_actions_collection", log)
    return collection, log


async def seed(collection):
    people = [
        ("ana@x.com", "es-MX", "instagram", "waiting", 3),
        ("bo@x.com", "en", "instagram", "invited", 1),
        ("cy@x.com", "fr", None, "joined", 2),
        ("di@x.com", "es", "friends", "left", 0),
        ("ed@x.com", None, "instagram", "waiting", 4),
        ("fa@x.com", "pt-BR", "invitation", "invited", 5),
    ]
    for email, locale, source, status, day in people:
        await collection.insert_one({
            "email": email, "locale": locale, "source": source, "status": status,
            "created_at": T0 + timedelta(days=day), "updated_at": T0, "leave_token": f"tok-{email}" * 3,
            **({"invited_at": T0 + timedelta(days=6)} if status in ("invited", "joined") else {}),
            **({"joined_at": T0 + timedelta(days=7)} if status == "joined" else {}),
        })


@pytest.mark.asyncio
async def test_rows_come_oldest_first_with_their_counts(rows):
    collection, log = rows
    await seed(collection)
    from app.routers import beta_admin

    answer = await beta_admin.list_waitlist(state=None, locale=None, source=None, limit=100, admin=ADMIN)
    assert isinstance(answer, WaitlistAdminResponse)
    assert [row.email for row in answer.rows] == ["di@x.com", "bo@x.com", "cy@x.com", "ana@x.com", "ed@x.com", "fa@x.com"]
    assert answer.totals == {"waiting": 2, "invited": 2, "joined": 1, "left": 1}
    assert answer.locales == {"es": 2, "en": 2, "fr": 1}
    assert answer.sources[0] == "instagram" and set(answer.sources) == {"instagram", "friends", "invitation"}
    assert log.rows == []


@pytest.mark.asyncio
async def test_a_row_shows_its_language_as_a_code_and_never_its_leave_token(rows):
    collection, _ = rows
    await seed(collection)
    answer = await waitlist_admin.list_waitlist(None, None, None, 100)
    by_email = {row["email"]: row for row in answer["rows"]}
    assert by_email["ana@x.com"]["locale"] == "es" and by_email["ed@x.com"]["locale"] is None
    assert by_email["fa@x.com"]["locale"] == "en"
    assert by_email["cy@x.com"]["joined_at"] == T0 + timedelta(days=7)
    assert by_email["bo@x.com"]["invited_at"] == T0 + timedelta(days=6) and by_email["bo@x.com"]["joined_at"] is None
    assert set(by_email["ana@x.com"]) == {"id", "email", "locale", "source", "status", "created_at", "invited_at", "joined_at"}
    assert "tok-" not in WaitlistAdminResponse(**answer).model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("state, locale, source, expected", [
    ("waiting", None, None, ["ana@x.com", "ed@x.com"]),
    ("left", None, None, ["di@x.com"]),
    (None, "es", None, ["di@x.com", "ana@x.com"]),
    (None, "en", None, ["bo@x.com", "fa@x.com"]),
    (None, "ja", None, []),
    (None, None, "instagram", ["bo@x.com", "ana@x.com", "ed@x.com"]),
    ("waiting", "es", "instagram", ["ana@x.com"]),
])
async def test_the_filters(rows, state, locale, source, expected):
    collection, _ = rows
    await seed(collection)
    answer = await waitlist_admin.list_waitlist(state, locale, source, 100)
    assert [row["email"] for row in answer["rows"]] == expected
    assert answer["totals"] == {"waiting": 2, "invited": 2, "joined": 1, "left": 1}


@pytest.mark.asyncio
async def test_the_list_is_bounded(rows):
    collection, _ = rows
    await seed(collection)
    answer = await waitlist_admin.list_waitlist(None, None, None, 2)
    assert [row["email"] for row in answer["rows"]] == ["di@x.com", "bo@x.com"]
    assert sum(answer["totals"].values()) == 6


@pytest.mark.asyncio
async def test_only_the_twenty_most_common_sources_are_offered(rows):
    collection, _ = rows
    for n in range(25):
        for k in range(n + 1):
            await collection.insert_one({"email": f"p{n}-{k}@x.com", "source": f"s{n:02d}", "status": "waiting", "created_at": T0})
    answer = await waitlist_admin.list_waitlist(None, None, None, 1)
    assert answer["sources"] == [f"s{n:02d}" for n in range(24, 4, -1)]


@pytest.mark.asyncio
async def test_an_empty_list(rows):
    answer = await waitlist_admin.list_waitlist(None, None, None, 100)
    assert answer == {"rows": [], "totals": {"waiting": 0, "invited": 0, "joined": 0, "left": 0}, "locales": {}, "sources": []}


def test_the_route_is_admin_only_and_its_query_is_bounded():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth.dependencies import require_admin
    from app.routers.beta_admin import router

    route = next(r for r in router.routes if r.path == "/beta/admin/waitlist")
    assert any(d.call is require_admin for d in route.dependant.dependencies)
    app = FastAPI()  # its own app, so the shared one keeps no override
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: ADMIN
    client = TestClient(app)
    for query in ("limit=0", "limit=201", "state=gone", "locale=pt", "source=" + "x" * 201):
        assert client.get(f"/beta/admin/waitlist?{query}").status_code == 422, query


def test_the_answer_model_refuses_an_unknown_state():
    with pytest.raises(ValidationError):
        WaitlistAdminResponse(rows=[{"id": "1", "email": "a@x.com", "status": "gone", "created_at": T0}],
                              totals={}, locales={}, sources=[])
