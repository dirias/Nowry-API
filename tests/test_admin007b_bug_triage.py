"""
ADMIN-007b — the Bug reports tab's API (docs/prd-admin.md FR-011, board C).

Pins:

  1. Statuses are a fixed list `new | in_progress | fixed | wont_fix`; stored old values read as
     open→new, in-progress→in_progress, resolved→fixed, closed→wont_fix, in rows, filters and counts.
  2. `PATCH /bugs/{id}/status` (recent admin) takes status, priority and a note; a note is appended
     to the history `{text, by, at}`, never replacing it; a legacy `dev_notes` reads as the first
     entry; only new status values are written; logged as `bug.status` with from, to, note_added.
  3. `GET /bugs/all` filters by status, severity, category, newest first, `limit` ≤ 200 (default 100);
     rows carry the reporter's username, never the address.
  4. `GET /bugs/{id}`: an admin reads any report as a row with its history; the reporter reads their
     own without the history; anyone else 403; a malformed id 400 `bad_id` on read, delete, change.
  5. `/bugs/my-reports` answers the caller's own reports with mapped statuses.
  6. `scripts/bug_statuses.py` counts old values in a dry run and rewrites them with `--apply`.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.models.Bug import current_status
from scripts import bug_statuses
from tests._bug_world import ADMIN, TESTER, World

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def stored_bug(**fields) -> dict:
    return {
        "user_id": str(TESTER), "title": "Review froze", "description": "The card did not flip.",
        "steps_to_reproduce": "", "severity": "high", "category": "study", "url": "",
        "browser_info": None, "platform": "android", "app_version": "1.0.30", "device": "Pixel 8",
        "screenshots": [], "status": "new", "priority": None, "notes": [], "created_at": NOW, "updated_at": NOW,
        **fields,
    }


@pytest.fixture
def world(monkeypatch):
    world = World(monkeypatch)
    world.act_as(ADMIN)
    return world


def seed(world, *bugs) -> list:
    return [str(i) for i in world.run(world.database["bugs"].insert_many(list(bugs))).inserted_ids]


@pytest.mark.parametrize("stored, read", [
    ("open", "new"), ("in-progress", "in_progress"), ("resolved", "fixed"), ("closed", "wont_fix"),
    ("new", "new"), ("wont_fix", "wont_fix"), (None, "new"), ("something", "new"),
])
def test_old_statuses_read_as_the_fixed_list(stored, read):
    assert current_status(stored) == read


def test_rows_filters_and_counts_read_old_values_as_new_ones(world):
    seed(world, stored_bug(status="open"), stored_bug(status="closed", severity="low"),
         stored_bug(status="new", created_at=NOW + timedelta(hours=1)))
    rows = world.client.get("/bugs/all").json()
    assert [row["status"] for row in rows] == ["new", "new", "wont_fix"]  # newest first
    assert len(world.client.get("/bugs/all", params={"status": "new"}).json()) == 2
    stats = world.client.get("/bugs/stats").json()
    assert stats["by_status"] == {"new": 2, "in_progress": 0, "fixed": 0, "wont_fix": 1}
    assert stats["by_severity"] == {"critical": 0, "high": 2, "medium": 0, "low": 1}


def test_a_row_carries_every_field_the_tab_shows_and_never_the_address(world):
    [bug_id] = seed(world, stored_bug(
        url="/calendar", platform="web", app_version=None, device=None,
        browser_info={"name": "Chrome", "version": "130", "os": "macOS", "screen_resolution": "1440x900"},
        screenshots=[{"filename": "a.png", "data": "data:image/png;base64,iVBO", "uploaded_at": NOW}],
    ))
    [row] = world.client.get("/bugs/all").json()
    assert row["id"] == bug_id and row["reporter_username"] == "tester_one"
    assert row["screen_size"] == "1440x900" and row["browser_info"]["name"] == "Chrome"
    assert row["screenshots"][0]["data"] == "data:image/png;base64,iVBO"
    for field in ("title", "description", "steps_to_reproduce", "severity", "category", "status", "priority",
                  "url", "platform", "app_version", "device", "created_at", "notes"):
        assert field in row
    assert "t@example.com" not in str(row) and "user_id" not in row


@pytest.mark.parametrize("query, expected", [
    ({"severity": "low"}, 1), ({"category": "books"}, 1), ({"status": "fixed"}, 0), ({"limit": 2}, 2),
])
def test_the_listing_filters_and_is_bounded(world, query, expected):
    seed(world, stored_bug(), stored_bug(severity="low"), stored_bug(category="books"))
    assert len(world.client.get("/bugs/all", params=query).json()) == expected


@pytest.mark.parametrize("query", [{"limit": 201}, {"limit": 0}, {"status": "open"}, {"category": "billing"}])
def test_the_listing_refuses_values_outside_its_lists(world, query):
    assert world.client.get("/bugs/all", params=query).status_code == 422


def test_notes_are_kept_as_a_history_after_a_legacy_note(world):
    [bug_id] = seed(world, stored_bug(status="in-progress", dev_notes="seen on Pixel only"))
    first = world.client.patch(f"/bugs/{bug_id}/status", json={"note": "reproduced"})
    assert first.status_code == 200 and first.json()["status"] == "in_progress"
    second = world.client.patch(f"/bugs/{bug_id}/status", json={"status": "fixed", "note": "fixed in 1.0.31", "priority": "high"})
    assert second.json()["status"] == "fixed" and second.json()["priority"] == "high"
    notes = world.client.get(f"/bugs/{bug_id}").json()["notes"]
    assert [n["text"] for n in notes] == ["seen on Pixel only", "reproduced", "fixed in 1.0.31"]
    assert notes[0]["by"] is None and notes[1]["by"] == str(ADMIN) and notes[1]["at"]
    assert second.json()["notes"] == notes
    [stored] = world.bugs()
    assert stored["status"] == "fixed" and stored["resolved_at"] and stored["dev_notes"] == "seen on Pixel only"


def test_a_change_is_logged_with_from_to_and_whether_a_note_was_added(world):
    [bug_id] = seed(world, stored_bug(status="open"))
    world.client.patch(f"/bugs/{bug_id}/status", json={"status": "wont_fix"})
    world.client.patch(f"/bugs/{bug_id}/status", json={"note": "duplicate of the other"})
    details = [(row["action"], row["details"]["from"], row["details"]["to"], row["details"]["note_added"]) for row in world.actions()]
    assert details == [("bug.status", "new", "wont_fix", False), ("bug.status", "wont_fix", "wont_fix", True)]
    assert world.bugs()[0]["status"] == "wont_fix"


def test_a_note_only_change_writes_the_new_name_of_an_old_status(world):
    [bug_id] = seed(world, stored_bug(status="resolved"))
    world.client.patch(f"/bugs/{bug_id}/status", json={"note": "verified"})
    assert world.bugs()[0]["status"] == "fixed"


@pytest.mark.parametrize("body", [{}, {"status": "resolved"}, {"note": "x" * 2001}, {"note": "   "}])
def test_a_change_outside_the_rules_is_422(world, body):
    [bug_id] = seed(world, stored_bug())
    assert world.client.patch(f"/bugs/{bug_id}/status", json=body).status_code == 422
    assert world.actions() == []


def test_the_reporter_reads_their_report_without_the_history_and_a_stranger_is_refused(world):
    [mine, theirs] = seed(world, stored_bug(notes=[{"text": "internal", "by": str(ADMIN), "at": NOW}]),
                          stored_bug(user_id=str(ADMIN)))
    world.act_as(TESTER)
    own = world.client.get(f"/bugs/{mine}")
    assert own.status_code == 200 and own.json()["notes"] == [] and own.json()["id"] == mine
    assert world.client.get(f"/bugs/{theirs}").status_code == 403
    assert world.client.get(f"/bugs/{ObjectId()}").status_code == 404


@pytest.mark.parametrize("method, path", [
    ("GET", "/bugs/not-an-id"), ("DELETE", "/bugs/not-an-id"), ("PATCH", "/bugs/not-an-id/status"),
])
def test_a_malformed_id_is_400_bad_id(world, method, path):
    response = world.client.request(method, path, json={"status": "fixed"} if method == "PATCH" else None)
    assert response.status_code == 400 and response.json()["detail"] == {"code": "bad_id"}


def test_my_reports_answers_only_the_callers_reports_with_new_status_names(world):
    seed(world, stored_bug(status="closed"), stored_bug(user_id=str(ADMIN)))
    world.act_as(TESTER)
    mine = world.client.get("/bugs/my-reports").json()
    assert [row["status"] for row in mine] == ["wont_fix"]


def run_script(argv, bugs) -> int:
    return asyncio.new_event_loop().run_until_complete(bug_statuses.run(bug_statuses.parse_args(argv), bugs=bugs))


def statuses_collection():
    bugs = AsyncMongoMockClient()["nowry-test"]["bugs"]
    rows = [{"status": s} for s in ("open", "open", "in-progress", "resolved", "closed", "new", "fixed")]
    asyncio.new_event_loop().run_until_complete(bugs.insert_many(rows))
    return bugs


def read_statuses(bugs) -> list:
    return sorted(r["status"] for r in asyncio.new_event_loop().run_until_complete(bugs.find({}).to_list(length=100)))


def test_the_status_script_counts_without_writing_by_default(capsys):
    bugs = statuses_collection()
    assert run_script([], bugs) == 0
    out = capsys.readouterr().out
    assert "open → new: 2" in out and "closed → wont_fix: 1" in out and "dry run" in out
    assert read_statuses(bugs) == sorted(["open", "open", "in-progress", "resolved", "closed", "new", "fixed"])


def test_the_status_script_rewrites_old_values_with_apply(capsys):
    bugs = statuses_collection()
    assert run_script(["--apply"], bugs) == 0
    assert read_statuses(bugs) == sorted(["new", "new", "in_progress", "fixed", "wont_fix", "new", "fixed"])
    assert "5 report(s) rewritten" in capsys.readouterr().out


def test_the_status_script_on_an_empty_collection(capsys):
    bugs = AsyncMongoMockClient()["nowry-test"]["bugs"]
    assert run_script(["--apply"], bugs) == 0
    assert "0 report(s) rewritten" in capsys.readouterr().out
