"""
ADMIN-002 — the report lists answer, and a malformed id is a 400 (docs/prd-admin.md FR-002).

Pins:

  1. `GET /moderation/admin/reports` and `GET /moderation/reports` answer through
     response models: every id is a string, so a stored ObjectId never turns into a 500.
  2. The reporter's own list never carries the reporter's address or account id.
  3. A malformed id on report, review or removal answers 400 `{"code": "bad_id"}`
     before anything is read or written.
  4. Reporting: once per account and item; only public content can be reported.

The real router over the fake world (mongomock), acting as real accounts.
"""
import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    from app.routers import moderation

    sent = []

    async def quiet_notify(subject, text, reply_to=None):
        sent.append(subject)
        return False

    monkeypatch.setattr(moderation, "notify", quiet_notify)
    with fake_world() as w:
        w.notices = sent
        yield w


def _public_deck(world, owner, name="Capitals"):
    oid = ObjectId()
    world.run(world.raw["decks"].insert_one({
        "_id": oid, "user_id": str(owner), "name": name, "is_public": True, "deleted_at": None,
    }))
    return str(oid)


def _report(world, deck_id, reason="spam", description=None):
    return world.http.post(f"/moderation/report/deck/{deck_id}", json={"reason": reason, "description": description})


def test_the_admin_list_answers_with_string_ids(world):
    owner, reporter, admin = world.new_user("owner"), world.new_user("reporter"), world.new_user("admin", is_admin=True)
    deck = _public_deck(world, owner)
    world.act_as(reporter)
    created = _report(world, deck, "copyright", "Copied from a textbook")
    assert created.status_code == 200, created.text
    report_id = created.json()["report_id"]
    assert ObjectId.is_valid(report_id)
    assert world.notices and world.notices[0].startswith("[Nowry] Content report")

    world.act_as(admin)
    answer = world.http.get("/moderation/admin/reports")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["total"] == 1 and body["page"] == 1 and body["total_pages"] == 1
    item = body["items"][0]
    assert item["id"] == report_id and isinstance(item["id"], str)
    assert item["content_id"] == deck and item["content_type"] == "deck" and item["content_title"] == "Capitals"
    assert item["reason"] == "copyright" and item["description"] == "Copied from a textbook"
    assert item["status"] == "pending" and item["reporter_user_id"] == str(reporter)
    assert "_id" not in item


def test_the_admin_list_filters_and_pages(world):
    owner, reporter, other = (world.new_user(n) for n in ("owner", "reporter", "other"))
    admin = world.new_user("admin", is_admin=True)
    first, second = _public_deck(world, owner, "One"), _public_deck(world, owner, "Two")
    world.act_as(reporter)
    _report(world, first, "spam")
    _report(world, second, "copyright")
    world.act_as(other)
    _report(world, first, "other")

    world.act_as(admin)
    by_reason = world.http.get("/moderation/admin/reports", params={"reason": "copyright"}).json()
    assert [r["content_title"] for r in by_reason["items"]] == ["Two"]
    paged = world.http.get("/moderation/admin/reports", params={"page_size": 2, "page": 2}).json()
    assert paged["total"] == 3 and paged["total_pages"] == 2 and len(paged["items"]) == 1


def test_my_reports_answer_with_string_ids_and_no_reporter_identity(world):
    owner, reporter = world.new_user("owner"), world.new_user("reporter")
    deck = _public_deck(world, owner)
    world.act_as(reporter)
    report_id = _report(world, deck).json()["report_id"]

    answer = world.http.get("/moderation/reports")
    assert answer.status_code == 200, answer.text
    item = answer.json()["items"][0]
    assert item["id"] == report_id and item["content_id"] == deck and item["status"] == "pending"
    assert "reporter_email" not in item and "reporter_user_id" not in item

    world.act_as(owner)
    assert world.http.get("/moderation/reports").json()["items"] == []


def test_the_admin_list_refuses_an_account_without_the_flag(world):
    world.act_as(world.new_user("someone"))
    assert world.http.get("/moderation/admin/reports").status_code == 403


def test_a_report_is_once_per_account_and_only_on_public_content(world):
    owner, reporter = world.new_user("owner"), world.new_user("reporter")
    deck = _public_deck(world, owner)
    private = ObjectId()
    world.run(world.raw["decks"].insert_one({"_id": private, "user_id": str(owner), "name": "Mine", "is_public": False}))
    world.act_as(reporter)
    assert _report(world, deck).status_code == 200
    assert _report(world, deck).status_code == 400
    assert _report(world, str(private)).status_code == 404


@pytest.mark.parametrize("method, path, body", [
    ("post", "/moderation/report/deck/not-an-id", {"reason": "spam"}),
    ("put", "/moderation/admin/reports/not-an-id/review", {"action_taken": "removed"}),
    ("delete", "/moderation/admin/content/deck/not-an-id?reason=spam", None),
])
def test_a_malformed_id_is_a_400_with_its_code(world, method, path, body):
    admin = world.new_user("admin", is_admin=True)
    world.act_as(admin)
    kwargs = {"json": body} if body is not None else {}
    answer = world.http.request(method.upper(), path, **kwargs)
    assert answer.status_code == 400, answer.text
    assert answer.json()["detail"] == {"code": "bad_id"}
    assert world.run(world.raw["content_reports"].count_documents({})) == 0
    assert world.run(world.raw["admin_actions"].count_documents({})) == 0
