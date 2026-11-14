"""
ADMIN-007c — the Overview and the admin log (docs/prd-admin.md FR-011 "Overview", boards A, K).

Pins:

  1. `GET /admin/overview` and `GET /admin/actions` are admin-only: an account without the flag is 403.
  2. The Overview's needs: open reports on decks and books (count, oldest, the two most common reasons);
     new bugs and how many of them are critical (old status names counted); open invitations ending within
     3 days and their two most common cohorts; waiting waitlist rows and how many arrived since the newest
     `invite.send` row (all when there is none).
  3. Today's email meters: the invitation and confirmation classes' sent rows today against their share
     of the day's budget (ADR-078), and the next UTC midnight.
  4. The recent actions: at most 8 rows of the last 7 days, newest first, shaped as the log's rows.
  5. The log: newest first, by kind (invitations, moderation, bugs, access), `before` an exclusive cursor,
     `limit` 1..100 (default 50), `next_before` only while more rows exist.
  6. A row names its actor by username (one query), "script" for the grant script, `is_you` for the caller;
     its details are a safe subset: never an address, a token, an owner id or a copy's list.
  7. The indexes those reads use exist.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.auth import dependencies
from app.auth.firebase_auth import get_firebase_user
from app.routers import admin as admin_router
from app.services import admin_log, admin_overview, moderation

ADMIN = ObjectId()
OTHER_ADMIN = ObjectId()
TESTER = ObjectId()
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class World:
    def __init__(self, monkeypatch) -> None:
        self.database = AsyncMongoMockClient()["nowry-test"]
        self.loop = asyncio.new_event_loop()
        monkeypatch.delenv("EMAIL_DAILY_BUDGET", raising=False)
        monkeypatch.setattr(dependencies, "users_collection", self.database["users"])
        monkeypatch.setattr(admin_log, "admin_actions_collection", self.database["admin_actions"])
        monkeypatch.setattr(admin_log, "users_collection", self.database["users"])
        monkeypatch.setattr(moderation, "db", self.database)
        for name in ("admin_actions", "bugs", "invites", "waitlist", "email_sends", "content_reports"):
            monkeypatch.setattr(admin_overview, f"{name}_collection", self.database[name])
        api = FastAPI()
        api.include_router(admin_router.router)
        self.caller: Dict[str, Any] = {"user_id": str(ADMIN)}
        api.dependency_overrides[get_firebase_user] = lambda: dict(self.caller)
        self.client = TestClient(api)
        self.run(self.database["users"].insert_many([
            {"_id": ADMIN, "email": "op@nowry.app", "username": "operator", "is_admin": True, "deleted_at": None},
            {"_id": OTHER_ADMIN, "email": "two@nowry.app", "username": "second", "is_admin": True, "deleted_at": None},
            {"_id": TESTER, "email": "t@example.com", "username": "tester_one", "deleted_at": None},
        ]))

    def run(self, awaitable):
        return self.loop.run_until_complete(awaitable)

    def seed(self, name: str, *rows: Dict[str, Any]) -> None:
        self.run(self.database[name].insert_many([dict(row) for row in rows]))

    def log(self, action: str, at: datetime, actor: Any = ADMIN, **fields: Any) -> None:
        row = {"actor_id": str(actor), "action": action, "target_type": "invite", "target_id": "t1",
               "target_title": None, "reason": None, "details": None, "created_at": at, **fields}
        self.seed("admin_actions", row)


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


# ── 1. Admin only ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/admin/overview", "/admin/actions"])
def test_both_reads_are_refused_to_an_account_without_the_flag(world, path):
    world.caller["user_id"] = str(TESTER)
    assert world.client.get(path).status_code == 403


# ── 2. What needs the admin ─────────────────────────────────────────────────


def _report(status: str, reason: str, age_hours: int, content_type: str = "deck", item: str = "") -> Dict[str, Any]:
    return {"content_type": content_type, "content_id": item or str(ObjectId()), "status": status, "reason": reason,
            "created_at": NOW - timedelta(hours=age_hours)}


def test_an_empty_world_answers_zeros_and_nulls(world):
    body = world.client.get("/admin/overview").json()
    assert body["needs"] == {
        "reports_to_review": {"count": 0, "oldest_at": None, "reasons": []},
        "new_bugs": {"count": 0, "critical": 0},
        "invitations_ending": {"count": 0, "cohorts": []},
        "waiting": {"count": 0, "since_last_batch": 0},
    }
    assert body["recent_actions"] == []


def test_reports_to_review_counts_items_as_the_moderation_tab_does_with_the_oldest_and_common_reasons(world):
    deck_a, deck_b, book_c, deck_d = (str(ObjectId()) for _ in range(4))
    world.seed(
        "content_reports",
        _report("pending", "spam", 30, item=deck_a), _report("under_review", "spam", 5, item=deck_a),
        _report("pending", "copyright", 2, item=deck_a), _report("pending", "copyright", 1, item=deck_b),
        _report("pending", "spam", 1, item=deck_b), _report("pending", "other", 3, "book", item=book_c),
        _report("resolved", "inappropriate", 90, item=deck_d), _report("dismissed", "inappropriate", 80, item=deck_d),
        _report("pending", "inappropriate", 99, "comment"),
    )
    reports = world.client.get("/admin/overview").json()["needs"]["reports_to_review"]
    assert reports["count"] == 3
    assert reports["count"] == world.run(moderation.queue_totals())["to_review"]
    assert reports["reasons"] == ["spam", "copyright"]
    oldest = datetime.fromisoformat(reports["oldest_at"].replace("Z", "+00:00"))
    assert oldest == NOW - timedelta(hours=30)


def test_new_bugs_count_old_names_and_the_critical_ones(world):
    world.seed(
        "bugs",
        {"status": "new", "severity": "critical"}, {"status": "open", "severity": "critical"},
        {"status": "new", "severity": "low"}, {"status": "in_progress", "severity": "critical"},
        {"status": "fixed", "severity": "high"},
    )
    assert world.client.get("/admin/overview").json()["needs"]["new_bugs"] == {"count": 3, "critical": 2}


def test_invitations_ending_are_open_and_end_within_three_days(world):
    def invite(cohort: str, ends_in: timedelta, status: str = "open") -> Dict[str, Any]:
        return {"email": f"{ObjectId()}@example.com", "cohort": cohort, "status": status, "expires_at": NOW + ends_in}

    world.seed(
        "invites",
        invite("friends", timedelta(days=1)), invite("friends", timedelta(hours=60)),
        invite("c1-waitlist", timedelta(days=2)), invite("press", timedelta(hours=70)),
        invite("friends", timedelta(days=4)), invite("friends", -timedelta(hours=1)),
        invite("friends", timedelta(days=1), "used"), invite("friends", timedelta(days=1), "revoked"),
    )
    ending = world.client.get("/admin/overview").json()["needs"]["invitations_ending"]
    assert ending["count"] == 4
    assert ending["cohorts"][0] == "friends" and len(ending["cohorts"]) == 2


def test_waiting_counts_rows_since_the_newest_invitation_batch(world):
    world.seed(
        "waitlist",
        {"email": "a@x.io", "status": "waiting", "created_at": NOW - timedelta(days=5)},
        {"email": "b@x.io", "status": "waiting", "created_at": NOW - timedelta(days=1)},
        {"email": "c@x.io", "status": "waiting", "created_at": NOW - timedelta(hours=1)},
        {"email": "d@x.io", "status": "invited", "created_at": NOW - timedelta(hours=1)},
    )
    assert world.client.get("/admin/overview").json()["needs"]["waiting"] == {"count": 3, "since_last_batch": 3}
    world.log("invite.send", NOW - timedelta(days=3))
    world.log("invite.send", NOW - timedelta(days=2))
    world.log("invite.revoke", NOW - timedelta(hours=2))
    assert world.client.get("/admin/overview").json()["needs"]["waiting"] == {"count": 3, "since_last_batch": 2}


# ── 3. Today's email meters ─────────────────────────────────────────────────


def test_the_meters_count_todays_sent_rows_against_each_class_share(world, monkeypatch):
    monkeypatch.setenv("EMAIL_DAILY_BUDGET", "10")
    today = NOW.strftime("%Y-%m-%d")
    yesterday = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    world.seed(
        "email_sends",
        {"class": "invitation", "outcome": "sent", "day": today},
        {"class": "invitation", "outcome": "sent", "day": today},
        {"class": "invitation", "outcome": "failed", "day": today},
        {"class": "invitation", "outcome": "sent", "day": yesterday},
        {"class": "confirmation", "outcome": "sent", "day": today},
        {"class": "account", "outcome": "sent", "day": today},
    )
    emails = world.client.get("/admin/overview").json()["emails_today"]
    assert emails["invitations"] == {"sent": 2, "share": 8}
    assert emails["confirmations"] == {"sent": 1, "share": 6}
    resets = datetime.fromisoformat(emails["resets_at"].replace("Z", "+00:00"))
    assert resets == datetime(NOW.year, NOW.month, NOW.day, tzinfo=timezone.utc) + timedelta(days=1)


def test_the_meters_use_the_default_budget_when_none_is_set(world):
    emails = world.client.get("/admin/overview").json()["emails_today"]
    assert emails["invitations"] == {"sent": 0, "share": 72}
    assert emails["confirmations"] == {"sent": 0, "share": 54}


def test_the_next_midnight_is_the_start_of_the_next_utc_day():
    late = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)
    assert admin_overview.next_utc_midnight(late) == datetime(2027, 1, 1, tzinfo=timezone.utc)


# ── 4. Recent actions ───────────────────────────────────────────────────────


def test_recent_actions_are_the_last_seven_days_newest_first_at_most_eight(world):
    for hours in range(1, 11):
        world.log("bug.status", NOW - timedelta(hours=hours), target_type="bug", target_id=f"b{hours}")
    world.log("bug.status", NOW - timedelta(days=8), target_type="bug", target_id="old")
    recent = world.client.get("/admin/overview").json()["recent_actions"]
    assert [row["target_id"] for row in recent] == [f"b{hours}" for hours in range(1, 9)]
    world.run(world.database["admin_actions"].delete_many({"target_id": {"$ne": "old"}}))
    assert world.client.get("/admin/overview").json()["recent_actions"] == []


# ── 5. The log ──────────────────────────────────────────────────────────────


def _seed_one_of_each(world) -> None:
    actions = ["invite.create", "invite.send", "invite.resend", "invite.revoke", "curation.review",
               "admin.grant", "admin.revoke", "bug.status", "report.review", "content.remove"]
    for minutes, action in enumerate(actions, start=1):
        world.log(action, NOW - timedelta(minutes=minutes))


@pytest.mark.parametrize("kind, actions", [
    ("invitations", ["invite.create", "invite.send", "invite.resend", "invite.revoke"]),
    ("moderation", ["curation.review", "report.review", "content.remove"]),
    ("bugs", ["bug.status"]),
    ("access", ["admin.grant", "admin.revoke"]),
])
def test_a_kind_reads_only_its_actions_newest_first(world, kind, actions):
    _seed_one_of_each(world)
    rows = world.client.get("/admin/actions", params={"kind": kind}).json()["actions"]
    assert sorted(row["action"] for row in rows) == sorted(actions)
    ats = [row["at"] for row in rows]
    assert ats == sorted(ats, reverse=True)


def test_no_kind_reads_every_action(world):
    _seed_one_of_each(world)
    body = world.client.get("/admin/actions").json()
    assert len(body["actions"]) == 10 and body["next_before"] is None
    assert body["actions"][0]["action"] == "invite.create"


def test_an_unknown_kind_a_limit_out_of_range_and_a_bad_cursor_are_422(world):
    assert world.client.get("/admin/actions", params={"kind": "everything"}).status_code == 422
    assert world.client.get("/admin/actions", params={"limit": 0}).status_code == 422
    assert world.client.get("/admin/actions", params={"limit": 101}).status_code == 422
    for bad in ("yesterday", "2026-11-14T12:00:00Z~not-an-id", "~" + str(ObjectId()), "a~b~c"):
        assert world.client.get("/admin/actions", params={"before": bad}).status_code == 422, bad


def _all_pages(world, limit: int, **params: Any) -> list:
    seen, query = [], {"limit": limit, **params}
    while True:
        body = world.client.get("/admin/actions", params=query).json()
        seen += body["actions"]
        if body["next_before"] is None:
            return seen
        query = {"limit": limit, **params, "before": body["next_before"]}


def test_pages_follow_the_cursor_without_repeating_or_skipping(world):
    for minutes in range(1, 8):
        world.log("bug.status", NOW - timedelta(minutes=minutes), target_id=f"r{minutes}")
    assert [row["target_id"] for row in _all_pages(world, 3)] == [f"r{minutes}" for minutes in range(1, 8)]


def test_a_removal_of_120_rows_at_one_instant_pages_whole_at_fifty(world):
    world.log("bug.status", NOW - timedelta(minutes=1), target_id="newer")
    world.seed("admin_actions", *[
        {"actor_id": str(ADMIN), "action": "content.remove", "target_type": "deck", "target_id": f"c{n}",
         "target_title": None, "reason": "copyright", "details": None, "created_at": NOW - timedelta(minutes=2)}
        for n in range(120)
    ])
    world.log("bug.status", NOW - timedelta(minutes=3), target_id="older")
    rows = _all_pages(world, 50)
    ids = [row["id"] for row in rows]
    assert len(rows) == 122 and len(set(ids)) == 122
    assert rows[0]["target_id"] == "newer" and rows[-1]["target_id"] == "older"
    same = [row["id"] for row in rows[1:121]]
    assert same == sorted(same, reverse=True)  # one instant: by id, newest first
    assert sorted(row["target_id"] for row in rows[1:121]) == sorted(f"c{n}" for n in range(120))


def test_a_kind_pages_through_one_instant_too(world):
    world.seed("admin_actions", *[
        {"actor_id": str(ADMIN), "action": "content.remove" if n % 2 else "bug.status", "target_type": "deck",
         "target_id": f"c{n}", "created_at": NOW - timedelta(minutes=2)}
        for n in range(30)
    ])
    rows = _all_pages(world, 4, kind="moderation")
    assert sorted(row["target_id"] for row in rows) == sorted(f"c{n}" for n in range(1, 30, 2))


def test_next_before_is_the_instant_and_the_id(world):
    for minutes in range(1, 4):
        world.log("bug.status", NOW - timedelta(minutes=minutes))
    body = world.client.get("/admin/actions", params={"limit": 2}).json()
    instant, row_id = body["next_before"].split("~")
    assert instant.endswith("Z") and row_id == body["actions"][-1]["id"]
    assert datetime.fromisoformat(instant.replace("Z", "+00:00")) == NOW - timedelta(minutes=2)


def test_a_plain_instant_reads_everything_before_it(world):
    world.log("bug.status", NOW - timedelta(minutes=1), target_id="after")
    world.log("bug.status", NOW - timedelta(minutes=2), target_id="at")
    world.log("bug.status", NOW - timedelta(minutes=3), target_id="before")
    instant = (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = world.client.get("/admin/actions", params={"before": instant}).json()["actions"]
    assert [row["target_id"] for row in rows] == ["before"]
    naive = (NOW - timedelta(minutes=2)).replace(tzinfo=None).isoformat()
    assert [row["target_id"] for row in world.client.get("/admin/actions", params={"before": naive}).json()["actions"]] == ["before"]


def test_a_full_last_page_says_there_is_no_next(world):
    for minutes in range(1, 4):
        world.log("bug.status", NOW - timedelta(minutes=minutes))
    assert world.client.get("/admin/actions", params={"limit": 3}).json()["next_before"] is None


def test_the_default_limit_is_fifty(world):
    for minutes in range(1, 56):
        world.log("bug.status", NOW - timedelta(minutes=minutes))
    body = world.client.get("/admin/actions").json()
    assert len(body["actions"]) == 50 and body["next_before"] is not None


# ── 6. Actors and details ───────────────────────────────────────────────────


def test_actors_are_named_by_username_and_the_caller_is_you(world):
    world.log("admin.grant", NOW - timedelta(minutes=1), actor="script", target_type="user")
    world.log("invite.send", NOW - timedelta(minutes=2), actor=OTHER_ADMIN)
    world.log("invite.send", NOW - timedelta(minutes=3), actor=ADMIN)
    world.log("invite.send", NOW - timedelta(minutes=4), actor=ObjectId())
    world.log("invite.send", NOW - timedelta(minutes=5), actor="not-an-id")
    actors = [row["actor"] for row in world.client.get("/admin/actions").json()["actions"]]
    assert actors == [
        {"kind": "script", "username": None, "is_you": False},
        {"kind": "admin", "username": "second", "is_you": False},
        {"kind": "admin", "username": "operator", "is_you": True},
        {"kind": "admin", "username": None, "is_you": False},
        {"kind": "admin", "username": None, "is_you": False},
    ]


def test_a_row_carries_its_target_and_reason(world):
    world.log("content.remove", NOW - timedelta(minutes=1), target_type="deck", target_id="d1",
              target_title="Kanji N5", reason="copyright")
    row = world.client.get("/admin/actions").json()["actions"][0]
    assert set(row) == {"id", "at", "actor", "action", "target_type", "target_id", "target_title", "reason", "details"}
    assert (row["target_type"], row["target_id"], row["target_title"], row["reason"]) == ("deck", "d1", "Kanji N5", "copyright")
    assert ObjectId.is_valid(row["id"])


def test_details_keep_only_the_safe_subset():
    details = {
        "cohort": "friends", "outcome": "sent", "from": "new", "to": "fixed", "note_added": True,
        "decision": "kept", "before": False, "after": True,
        "copies": [{"content_id": "c1", "owner_id": "u1", "title": "x"}, {"content_id": "c2"}],
        "note": "private words", "report_id": "r1", "owner_told": "sent", "replaces": "i0",
        "token": "nwi_secret", "email": "ana@example.com", "report_ids": ["r1"], "priority": "high",
    }
    assert admin_log.safe_details(details) == {
        "cohort": "friends", "outcome": "sent", "from": "new", "to": "fixed", "note_added": True,
        "decision": "kept", "before": False, "after": True, "copies": 2,
    }


@pytest.mark.parametrize("details", [None, "not a dict", {}])
def test_details_that_are_missing_or_odd_read_as_empty(details):
    assert admin_log.safe_details(details) == {}


def test_an_unsafe_value_under_a_safe_key_is_dropped():
    assert admin_log.safe_details({"cohort": {"nested": "x"}, "from": ["a"], "to": "fixed"}) == {"to": "fixed"}


def test_the_log_never_answers_an_address_from_details(world):
    world.log("invite.resend", NOW - timedelta(minutes=1), target_title="ana@example.com",
              details={"replaces": "replaced-invite", "state_before": "open", "cohort": "friends", "outcome": "sent",
                       "email": "other@example.com"})
    text = world.client.get("/admin/actions").text
    assert "other@example.com" not in text and "replaced-invite" not in text and "ana@example.com" in text


@pytest.mark.parametrize("action, kind", [
    ("invite.send", "invitations"), ("report.review", "moderation"), ("content.remove", "moderation"),
    ("curation.review", "moderation"), ("bug.status", "bugs"), ("admin.revoke", "access"),
])
def test_every_action_has_one_kind(action, kind):
    assert admin_log.kind_of(action) == kind


def test_the_kinds_cover_the_whole_action_list():
    from app.services.admin_actions import ADMIN_ACTIONS

    listed = [action for actions in admin_log.KINDS.values() for action in actions]
    assert sorted(listed) == sorted(ADMIN_ACTIONS)


# ── 7. Indexes ──────────────────────────────────────────────────────────────


def test_the_reads_have_their_indexes():
    from tests._fake_world import fake_world

    with fake_world() as world:
        import app.config.database as database

        world.run(database.create_indexes())

        def keys(name: str) -> Dict[str, list]:
            info = world.run(world.raw[name].index_information())
            return {index: [tuple(k) for k in spec["key"]] for index, spec in info.items()}

        assert keys("admin_actions")["admin_actions_action"] == [("action", 1), ("created_at", -1), ("_id", -1)]
        assert keys("admin_actions")["admin_actions_newest"] == [("created_at", -1), ("_id", -1)]
        assert keys("invites")["invites_ending"] == [("status", 1), ("expires_at", 1)]
        assert keys("waitlist")["waitlist_status_created"] == [("status", 1), ("created_at", 1)]
        assert keys("bugs")["bugs_status_severity"] == [("status", 1), ("severity", 1)]
        assert keys("email_sends")["email_sends_day_class"] == [("day", 1), ("class", 1), ("outcome", 1)]


def test_a_copy_row_is_flagged_without_its_source_id():
    assert admin_log.safe_details({"copy_of": "65f0a1b2c3d4e5f6a7b8c9d0", "note": "x"}) == {"is_copy": True}
    assert "is_copy" not in admin_log.safe_details({"copies": [{"content_id": "a"}]})


@pytest.mark.asyncio
async def test_the_overview_leaves_a_removals_copies_to_its_own_row(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[])
    find = MagicMock(return_value=cursor)
    monkeypatch.setattr(admin_log.admin_actions_collection, "find", find)
    from datetime import datetime, timezone
    await admin_log.recent_actions(datetime(2026, 11, 8, tzinfo=timezone.utc), 8, None)
    assert find.call_args.args[0]["details.copy_of"] == {"$exists": False}
