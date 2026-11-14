"""
ADMIN-001 — one admin check on the bug routes; the news-cache route removed
(ADR-086 point 1, docs/prd-admin.md FR-001).

Pins:

  1. `GET /bugs/all`, `GET /bugs/stats` and `PATCH /bugs/{id}/status` answer 403 to an
     account without `is_admin`, whatever its `role` (the old "dev"/"beta" pass is gone),
     and never 500 — the `status` query parameter no longer hides FastAPI's module.
  2. An admin reads the list (ids as strings, filters applied) and the counts.
  3. An admin's status change is written and logged as `bug.status`.
  4. A reporter still reads and deletes only their own reports.
  5. `DELETE /news/cache/clear` no longer exists.
"""
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.auth import dependencies
from app.auth.firebase_auth import get_firebase_user
from app.routers import bugs as bugs_router
from app.services import admin_actions

ADMIN = ObjectId()
TESTER = ObjectId()
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def bug(user_id: ObjectId, **fields) -> dict:
    return {
        "user_id": str(user_id), "title": "The calendar lost a milestone", "description": "Gone after reload.",
        "severity": "high", "category": "functionality", "url": "/calendar",
        "browser_info": {"name": "Chrome", "version": "130", "os": "macOS", "screen_resolution": "1440x900"},
        "screenshots": [], "status": "open", "created_at": NOW, "updated_at": NOW, **fields,
    }


@pytest.fixture
def world(monkeypatch):
    database = AsyncMongoMockClient()["nowry-test"]
    monkeypatch.setattr(dependencies, "users_collection", database["users"])
    monkeypatch.setattr(bugs_router, "bugs_collection", database["bugs"])
    monkeypatch.setattr(bugs_router, "users_collection", database["users"])
    monkeypatch.setattr(admin_actions, "admin_actions_collection", database["admin_actions"])
    api = FastAPI()
    api.include_router(bugs_router.router)
    caller = {"user_id": str(TESTER)}
    api.dependency_overrides[get_firebase_user] = lambda: {"auth_time": int(datetime.now(timezone.utc).timestamp()), **caller}
    client = TestClient(api)

    async def seed():
        await database["users"].insert_many([
            {"_id": ADMIN, "email": "op@nowry.app", "is_admin": True, "deleted_at": None},
            {"_id": TESTER, "email": "t@example.com", "role": "dev", "deleted_at": None},
        ])
        await database["bugs"].insert_many([bug(TESTER), bug(TESTER, status="resolved", severity="low", created_at=NOW - timedelta(days=1))])

    import asyncio

    asyncio.new_event_loop().run_until_complete(seed())  # mongomock is loop-agnostic
    return client, caller, database


def act_as(caller: dict, who: ObjectId) -> None:
    caller["user_id"] = str(who)


@pytest.mark.parametrize("method, path", [
    ("GET", "/bugs/all"),
    ("GET", "/bugs/all?status=open"),
    ("GET", "/bugs/stats"),
    ("PATCH", f"/bugs/{ObjectId()}/status"),
])
def test_an_account_without_the_flag_is_refused_403_whatever_its_role(world, method, path):
    client, caller, _ = world
    act_as(caller, TESTER)  # role "dev": that used to pass
    response = client.request(method, path, json={"status": "resolved"} if method == "PATCH" else None)
    assert response.status_code == 403


def test_an_admin_reads_every_report_with_string_ids_and_filters(world):
    client, caller, _ = world
    act_as(caller, ADMIN)
    everything = client.get("/bugs/all")
    assert everything.status_code == 200
    assert len(everything.json()) == 2 and all(isinstance(row["_id"], str) for row in everything.json())
    resolved = client.get("/bugs/all", params={"status": "resolved"}).json()
    assert [row["status"] for row in resolved] == ["resolved"]


def test_an_admin_reads_the_counts(world):
    client, caller, _ = world
    act_as(caller, ADMIN)
    stats = client.get("/bugs/stats").json()
    assert stats == {
        "total": 2,
        "by_status": {"open": 1, "in_progress": 0, "resolved": 1},
        "by_severity": {"critical": 0, "high": 1, "medium": 0, "low": 1},
    }


def test_an_admin_status_change_is_written_and_logged(world):
    import asyncio

    client, caller, database = world
    act_as(caller, ADMIN)
    target = client.get("/bugs/all", params={"status": "open"}).json()[0]["_id"]
    response = client.patch(f"/bugs/{target}/status", json={"status": "resolved", "notes": "fixed in 1.2"})
    assert response.status_code == 200 and response.json()["status"] == "resolved"

    loop = asyncio.new_event_loop()
    stored = loop.run_until_complete(database["bugs"].find_one({"_id": ObjectId(target)}))
    assert stored["status"] == "resolved" and stored["dev_notes"] == "fixed in 1.2" and stored["resolved_at"]
    rows = loop.run_until_complete(database["admin_actions"].find({}).to_list(length=10))
    assert [(r["actor_id"], r["action"], r["target_id"]) for r in rows] == [(str(ADMIN), "bug.status", target)]
    assert rows[0]["details"]["from"] == "open" and rows[0]["details"]["to"] == "resolved"


def test_a_malformed_or_unknown_bug_id_on_the_status_change(world):
    client, caller, _ = world
    act_as(caller, ADMIN)
    assert client.patch("/bugs/not-an-id/status", json={"status": "resolved"}).status_code == 400
    assert client.patch(f"/bugs/{ObjectId()}/status", json={"status": "resolved"}).status_code == 404


def test_a_reporter_reads_only_their_own_reports(world):
    client, caller, _ = world
    act_as(caller, TESTER)
    mine = client.get("/bugs/my-reports").json()
    assert len(mine) == 2
    act_as(caller, ADMIN)
    assert client.get(f"/bugs/{mine[0]['_id']}").status_code == 403


def test_the_news_cache_route_is_gone():
    from app.routers import news

    assert not any("cache/clear" in getattr(route, "path", "") for route in news.router.routes)


def test_a_stale_admin_sign_in_is_asked_again_before_a_status_change(world):
    """ADMIN-008b: an admin write needs a sign-in less than 12 hours old; reads do not."""
    client, caller, _ = world
    act_as(caller, ADMIN)
    caller["auth_time"] = int(datetime.now(timezone.utc).timestamp()) - 13 * 3600
    target = client.get("/bugs/all").json()[0]["_id"]
    response = client.patch(f"/bugs/{target}/status", json={"status": "resolved"})
    assert response.status_code == 401 and response.json()["detail"] == {"code": "reauth_required"}
