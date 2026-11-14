"""
ADMIN-003 — a removal is final, reaches public copies, and is explained
(docs/prd-admin.md FR-004..FR-006, ADR-087), and the Moderation tab's queue.

Pins:

  1. The queue: `to_review` (open reports, oldest report first), `kept` (every report
     dismissed), `removed` (a removal marker, newest first; copies are not listed);
     each item carries its size, learners, public copies and reports without the
     reporter; totals per state.
  2. Keep public dismisses every open report on the item and logs one `report.review` row.
  3. Unpublish is one operation: the item and every PUBLIC copy (by `forked_from.id`,
     3 levels, 200 items) lose `is_public` and gain the removal marker (`copy_of` on a
     copy); private copies are untouched; open reports are resolved; one `content.remove`
     row per item; one `content.removed` email per owner, verified addresses only,
     the outcome on the row; the operator notice `[Nowry] Removed: …`.
  4. The review route and the direct removal route go through the same operations.
  5. Publishing refuses a removed deck or book (`removed_by_moderation`, ADR-037 path);
     the side doors refuse too, and private editing still works.
  6. The `content.removed` email in five languages, both variants, no button, no leave link.

The real app over the fake world (mongomock), acting as real accounts. Email and the
operator notice are recorded, never sent.
"""
import time
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

T0 = datetime(2026, 11, 1, 9, 0)


@pytest.fixture
def world(monkeypatch):
    from app.services import moderation as moderation_service

    mails, notices, outcomes = [], [], {}

    async def record_email(kind, to, locale, values, headers=None):
        mails.append({"kind": kind, "to": to, "locale": locale, "values": dict(values)})
        return outcomes.get(to, "sent")

    async def record_notice(subject, text, reply_to=None):
        notices.append((subject, text))
        return True

    monkeypatch.setattr(moderation_service, "send_email", record_email)
    monkeypatch.setattr(moderation_service, "notify", record_notice)
    with fake_world() as w:
        w.mails, w.notices, w.outcomes = mails, notices, outcomes
        yield w


def _person(world, label, language="en", verified=True, **extra):
    return world.new_user(label, email_verified=verified, preferences={"general": {"language": language}}, **extra)


def _deck(world, owner, name, public=True, copy_of=None, published_at=T0, **extra):
    oid = ObjectId()
    doc = {"_id": oid, "user_id": str(owner), "name": name, "is_public": public, "deleted_at": None,
           "published_at": published_at if public else None, **extra}
    if copy_of:
        doc["forked_from"] = {"id": copy_of, "title": "source", "author_name": "x", "author_id": "y"}
    world.run(world.raw["decks"].insert_one(doc))
    return str(oid)


def _book(world, owner, title, public=True, **extra):
    oid = ObjectId()
    world.run(world.raw["books"].insert_one({"_id": oid, "user_id": str(owner), "title": title, "is_public": public,
                                             "deleted_at": None, "published_at": T0, **extra}))
    return str(oid)


def _cards(world, deck_id, count):
    for n in range(count):
        world.run(world.raw["cards"].insert_one({"deck_id": deck_id, "user_id": "x", "title": f"c{n}", "deleted_at": None}))


def _open_report(world, content_type, content_id, reporter, reason="copyright", created=T0, status="pending", note=None):
    oid = ObjectId()
    world.run(world.raw["content_reports"].insert_one({
        "_id": oid, "content_type": content_type, "content_id": content_id, "content_title": "t",
        "reporter_user_id": str(reporter), "reporter_email": "secret@example.com", "reason": reason,
        "description": note, "status": status, "created_at": created, "updated_at": created,
    }))
    return str(oid)


def _doc(world, collection, oid):
    return world.run(world.raw[collection].find_one({"_id": ObjectId(oid)}))


def _log(world, action=None):
    query = {"action": action} if action else {}
    return world.run(world.raw["admin_actions"].find(query).to_list(length=100))


@pytest.fixture
def admin(world):
    uid = world.new_user("admin", is_admin=True)
    return uid


# ── 1. The queue ─────────────────────────────────────────────────────────────


def test_to_review_lists_items_with_open_reports_oldest_first(world, admin):
    owner, learner, reporter = _person(world, "ana"), _person(world, "bo"), _person(world, "cy")
    newer = _deck(world, owner, "Newer")
    older = _deck(world, owner, "Older", published_at=T0 - timedelta(days=3))
    _cards(world, older, 3)
    _deck(world, learner, "Older (Forked)", public=True, copy_of=older)
    _deck(world, reporter, "Older (Forked)", public=False, copy_of=older)
    book = _book(world, owner, "Notes", page_count=12)
    _open_report(world, "deck", newer, reporter, created=T0 + timedelta(days=2))
    first = _open_report(world, "deck", older, reporter, "spam", created=T0, note="ads everywhere")
    _open_report(world, "deck", older, learner, "other", created=T0 + timedelta(hours=1))
    _open_report(world, "book", book, reporter, created=T0 + timedelta(days=1))
    _open_report(world, "card", str(ObjectId()), reporter)  # not a queue item
    _open_report(world, "deck", newer, learner, status="dismissed")

    world.act_as(admin)
    answer = world.http.get("/moderation/admin/queue")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["totals"] == {"to_review": 3, "kept": 0, "removed": 0}
    assert [item["title"] for item in body["items"]] == ["Older", "Notes", "Newer"]
    item = body["items"][0]
    assert item["content_type"] == "deck" and item["content_id"] == older and item["is_public"] is True
    assert item["owner_username"].startswith("ana") and item["public_since"].startswith("2026-10-29")
    assert (item["size"], item["learners"], item["public_copies"]) == (3, 2, 1)
    assert item["report_count"] == 2 and item["oldest_report_at"].startswith("2026-11-01T09:00")
    assert item["reports"][0] == {"id": first, "reason": "spam", "note": "ads everywhere", "created_at": item["oldest_report_at"]}
    assert item["removal"] is None
    assert "secret@example.com" not in answer.text and str(reporter) not in answer.text
    assert body["items"][1]["size"] == 12 and body["items"][2]["report_count"] == 1


def test_the_queue_is_bounded_and_admin_only(world, admin):
    owner, reporter = _person(world, "ana"), _person(world, "cy")
    for n in range(3):
        _open_report(world, "deck", _deck(world, owner, f"D{n}"), reporter, created=T0 + timedelta(hours=n))
    world.act_as(admin)
    body = world.http.get("/moderation/admin/queue", params={"limit": 2}).json()
    assert [item["title"] for item in body["items"]] == ["D0", "D1"] and body["totals"]["to_review"] == 3
    assert world.http.get("/moderation/admin/queue", params={"limit": 101}).status_code == 422
    assert world.http.get("/moderation/admin/queue", params={"state": "later"}).status_code == 422
    world.act_as(reporter)
    assert world.http.get("/moderation/admin/queue").status_code == 403


# ── 2. Keep public ───────────────────────────────────────────────────────────


def test_keep_public_dismisses_every_open_report_and_logs_once(world, admin):
    owner, reporter, other = _person(world, "ana"), _person(world, "cy"), _person(world, "di")
    deck = _deck(world, owner, "Capitals")
    ids = [_open_report(world, "deck", deck, reporter), _open_report(world, "deck", deck, other, "spam")]
    world.act_as(admin)

    answer = world.http.post(f"/moderation/admin/queue/deck/{deck}/keep")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"dismissed": 2}
    for rid in ids:
        report = _doc(world, "content_reports", rid)
        assert report["status"] == "dismissed" and report["reviewed_by"] == str(admin) and report["reviewed_at"]
    assert _doc(world, "decks", deck)["is_public"] is True
    rows = _log(world)
    assert len(rows) == 1 and rows[0]["action"] == "report.review" and rows[0]["target_id"] == deck
    assert rows[0]["details"] == {"decision": "kept", "report_ids": ids}

    body = world.http.get("/moderation/admin/queue", params={"state": "kept"}).json()
    assert body["totals"] == {"to_review": 0, "kept": 1, "removed": 0}
    assert body["items"][0]["content_id"] == deck and body["items"][0]["report_count"] == 2
    assert world.http.post(f"/moderation/admin/queue/deck/{deck}/keep").json() == {"dismissed": 0}
    assert len(_log(world)) == 1


def test_an_admin_write_needs_a_recent_sign_in(world, admin):
    world.act_as(admin)
    world.current["user"] = {**world.users[admin], "auth_time": int(time.time()) - 13 * 3600}
    deck = str(ObjectId())
    assert world.http.post(f"/moderation/admin/queue/deck/{deck}/keep").status_code == 401
    assert world.http.post(f"/moderation/admin/queue/deck/{deck}/remove", json={"reason": "spam"}).status_code == 401


# ── 3. Unpublish ─────────────────────────────────────────────────────────────


@pytest.fixture
def family(world, admin):
    """Batch 3 step 2: A publishes X; B's public copy; C's private copy; D's public copy of B's copy."""
    a, b = _person(world, "ana", "es"), _person(world, "bo", "fr")
    c, d = _person(world, "cy", "de"), _person(world, "di", "ja")
    x = _deck(world, a, "Pharmacology")
    b_copy = _deck(world, b, "Pharmacology (Forked)", copy_of=x)
    c_copy = _deck(world, c, "Pharmacology (Forked)", public=False, copy_of=x)
    d_copy = _deck(world, d, "Pharmacology (Forked) (Forked)", copy_of=b_copy)
    old = _open_report(world, "deck", x, c, "copyright", created=T0)
    new = _open_report(world, "deck", x, d, "spam", created=T0 + timedelta(hours=2))
    return {"a": a, "b": b, "c": c, "d": d, "x": x, "b_copy": b_copy, "c_copy": c_copy, "d_copy": d_copy,
            "reports": [old, new]}


def test_unpublish_takes_the_item_and_its_public_copies(world, admin, family):
    f = family
    world.act_as(admin)
    answer = world.http.post(f"/moderation/admin/queue/deck/{f['x']}/remove", json={"reason": "copyright", "note": "Publisher's notice"})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["removed"][0] == {"content_type": "deck", "content_id": f["x"], "title": "Pharmacology"}
    assert {r["content_id"] for r in body["removed"][1:]} == {f["b_copy"], f["d_copy"]}
    assert (body["copies_removed"], body["owners_told"], body["owners_not_told"]) == (2, 3, 0)

    item = _doc(world, "decks", f["x"])
    assert item["is_public"] is False
    marker = item["removal"]
    assert marker["reason"] == "copyright" and marker["note"] == "Publisher's notice"
    assert marker["removed_by"] == str(admin) and marker["report_id"] == f["reports"][1] and marker["removed_at"]
    assert "copy_of" not in marker
    for copy in (f["b_copy"], f["d_copy"]):
        stored = _doc(world, "decks", copy)
        assert stored["is_public"] is False and stored["removal"]["copy_of"] == f["x"]
        assert stored["removal"]["reason"] == "copyright"
    private = _doc(world, "decks", f["c_copy"])
    assert "removal" not in private and private["is_public"] is False
    for rid in f["reports"]:
        assert _doc(world, "content_reports", rid)["status"] == "resolved"


def test_unpublish_logs_one_row_per_item_and_the_copies_on_the_items_row(world, admin, family):
    f = family
    world.act_as(admin)
    world.http.post(f"/moderation/admin/queue/deck/{f['x']}/remove", json={"reason": "copyright"})
    rows = {row["target_id"]: row for row in _log(world, "content.remove")}
    assert set(rows) == {f["x"], f["b_copy"], f["d_copy"]} and len(_log(world)) == 3
    main = rows[f["x"]]
    assert main["reason"] == "copyright" and main["actor_id"] == str(admin) and main["target_title"] == "Pharmacology"
    assert {c["content_id"] for c in main["details"]["copies"]} == {f["b_copy"], f["d_copy"]}
    assert main["details"]["owner_told"] == "sent"
    assert rows[f["b_copy"]]["details"]["copy_of"] == f["x"]


def test_each_owner_is_told_once_in_their_language(world, admin, family):
    f = family
    world.act_as(admin)
    world.http.post(f"/moderation/admin/queue/deck/{f['x']}/remove", json={"reason": "copyright"})
    by_to = {mail["to"]: mail for mail in world.mails}
    assert len(world.mails) == 3 and all(mail["kind"] == "content.removed" for mail in world.mails)
    assert set(by_to) == {"ana@example.com", "bo@example.com", "di@example.com"}
    assert (by_to["ana@example.com"]["locale"], by_to["bo@example.com"]["locale"], by_to["di@example.com"]["locale"]) == ("es", "fr", "ja")
    assert "copia" not in by_to["ana@example.com"]["values"]["lead"]
    assert "copie" in by_to["bo@example.com"]["values"]["lead"]
    assert by_to["ana@example.com"]["values"]["note_line"] == ""
    subject, text = world.notices[0]
    assert len(world.notices) == 1 and subject == "[Nowry] Removed: Pharmacology"
    assert "copyright" in text and f["b_copy"] in text


def test_an_unverified_or_refused_owner_is_recorded_not_told(world, admin):
    owner, copier, reporter = _person(world, "ana", verified=False), _person(world, "bo"), _person(world, "cy")
    deck = _deck(world, owner, "Capitals")
    copy = _deck(world, copier, "Capitals (Forked)", copy_of=deck)
    world.outcomes["bo@example.com"] = "over_budget"
    _open_report(world, "deck", deck, reporter)
    world.act_as(admin)
    body = world.http.post(f"/moderation/admin/queue/deck/{deck}/remove", json={"reason": "spam"}).json()
    assert (body["owners_told"], body["owners_not_told"]) == (0, 2)
    assert [mail["to"] for mail in world.mails] == ["bo@example.com"]
    rows = {row["target_id"]: row for row in _log(world, "content.remove")}
    assert rows[deck]["details"]["owner_not_told"] == "unverified"
    assert rows[copy]["details"]["owner_not_told"] == "over_budget"


def test_the_copy_walk_stops_at_three_levels(world, admin):
    owner = _person(world, "ana")
    chain = [_deck(world, owner, "L0")]
    for level in range(1, 5):
        chain.append(_deck(world, owner, f"L{level}", copy_of=chain[-1]))
    world.act_as(admin)
    body = world.http.post(f"/moderation/admin/queue/deck/{chain[0]}/remove", json={"reason": "spam"}).json()
    assert body["copies_removed"] == 3
    assert _doc(world, "decks", chain[3])["is_public"] is False
    assert _doc(world, "decks", chain[4])["is_public"] is True


def test_a_copy_recorded_by_original_id_is_found_too(world, admin):
    owner = _person(world, "ana")
    deck = _deck(world, owner, "Capitals")
    legacy = _deck(world, owner, "Old copy", forked_from={"original_id": deck, "original_title": "Capitals"})
    world.act_as(admin)
    assert world.http.post(f"/moderation/admin/queue/deck/{deck}/remove", json={"reason": "spam"}).json()["copies_removed"] == 1
    assert _doc(world, "decks", legacy)["removal"]["copy_of"] == deck


def test_a_book_is_removed_the_same_way(world, admin):
    owner = _person(world, "ana")
    book = _book(world, owner, "Notes")
    world.act_as(admin)
    body = world.http.post(f"/moderation/admin/queue/book/{book}/remove", json={"reason": "inappropriate"}).json()
    assert body["removed"] == [{"content_type": "book", "content_id": book, "title": "Notes"}]
    assert _doc(world, "books", book)["removal"]["reason"] == "inappropriate"
    assert world.mails[0]["values"]["title"] == "Notes"


def test_removed_is_final_and_listed_newest_first(world, admin, family):
    f = family
    world.act_as(admin)
    world.http.post(f"/moderation/admin/queue/deck/{f['x']}/remove", json={"reason": "copyright"})
    world.run(world.raw["decks"].update_one({"_id": ObjectId(f["x"])}, {"$set": {"removal.removed_at": T0}}))
    later = _deck(world, f["a"], "Later")
    world.http.post(f"/moderation/admin/queue/deck/{later}/remove", json={"reason": "spam", "note": "n"})
    again = world.http.post(f"/moderation/admin/queue/deck/{f['x']}/remove", json={"reason": "spam"})
    assert again.status_code == 409 and again.json()["detail"] == {"code": "already_removed"}
    missing = world.http.post(f"/moderation/admin/queue/deck/{ObjectId()}/remove", json={"reason": "spam"})
    assert missing.status_code == 404

    body = world.http.get("/moderation/admin/queue", params={"state": "removed"}).json()
    assert body["totals"] == {"to_review": 0, "kept": 0, "removed": 2}
    assert [item["title"] for item in body["items"]] == ["Later", "Pharmacology"]
    assert body["items"][0]["removal"]["reason"] == "spam" and body["items"][0]["removal"]["note"] == "n"
    assert body["items"][1]["report_count"] == 2 and body["items"][1]["is_public"] is False


def test_the_removal_body_is_checked(world, admin):
    deck = _deck(world, _person(world, "ana"), "Capitals")
    world.act_as(admin)
    assert world.http.post(f"/moderation/admin/queue/deck/{deck}/remove", json={"reason": "rude"}).status_code == 422
    long_note = {"reason": "other", "note": "x" * 501}
    assert world.http.post(f"/moderation/admin/queue/deck/{deck}/remove", json=long_note).status_code == 422
    assert world.http.post(f"/moderation/admin/queue/card/{deck}/remove", json={"reason": "spam"}).status_code == 422
    assert _doc(world, "decks", deck)["is_public"] is True


@pytest.mark.parametrize("path, body", [
    ("/moderation/admin/queue/deck/not-an-id/keep", None),
    ("/moderation/admin/queue/book/12345/remove", {"reason": "spam"}),
])
def test_a_malformed_queue_id_is_a_400(world, admin, path, body):
    world.act_as(admin)
    answer = world.http.post(path, json=body) if body else world.http.post(path)
    assert answer.status_code == 400 and answer.json()["detail"] == {"code": "bad_id"}


# ── 4. The review route and the direct route ────────────────────────────────


def test_the_review_keeps_or_removes_through_the_same_operations(world, admin, family):
    f = family
    world.act_as(admin)
    kept = world.http.put(f"/moderation/admin/reports/{f['reports'][0]}/review", json={"action": "kept"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["dismissed"] == 2 and kept.json()["action"] == "kept"
    again = world.http.put(f"/moderation/admin/reports/{f['reports'][0]}/review", json={"action": "removed"})
    assert again.status_code == 409 and again.json()["detail"] == {"code": "already_reviewed"}

    fresh = _open_report(world, "deck", f["x"], f["b"], "misinformation")
    removed = world.http.put(f"/moderation/admin/reports/{fresh}/review", json={"action_taken": "removed"})
    assert removed.status_code == 200, removed.text
    assert removed.json()["removal"]["copies_removed"] == 2
    marker = _doc(world, "decks", f["x"])["removal"]
    assert marker["report_id"] == fresh and marker["reason"] == "misinformation"
    assert _doc(world, "content_reports", fresh)["status"] == "resolved"
    assert world.http.put(f"/moderation/admin/reports/{ObjectId()}/review", json={"action": "kept"}).status_code == 404
    old_value = world.http.put(f"/moderation/admin/reports/{fresh}/review", json={"action_taken": "kept_public"})
    assert old_value.status_code == 422


def test_a_card_report_can_be_kept_but_not_removed(world, admin):
    reporter = _person(world, "cy")
    report = _open_report(world, "card", str(ObjectId()), reporter)
    world.act_as(admin)
    refused = world.http.put(f"/moderation/admin/reports/{report}/review", json={"action": "removed"})
    assert refused.status_code == 400 and refused.json()["detail"] == {"code": "not_removable"}
    assert world.http.put(f"/moderation/admin/reports/{report}/review", json={"action": "kept"}).json()["dismissed"] == 1


def test_the_direct_route_is_the_same_removal(world, admin, family):
    f = family
    world.act_as(admin)
    assert world.http.delete(f"/moderation/admin/content/deck/{f['x']}", params={"reason": "rude"}).status_code == 422
    answer = world.http.delete(f"/moderation/admin/content/deck/{f['x']}", params={"reason": "spam", "note": "dup"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["copies_removed"] == 2
    assert _doc(world, "decks", f["x"])["removal"]["note"] == "dup"
    assert len(_log(world, "content.remove")) == 3


# ── 5. Publishing refuses a removed item ────────────────────────────────────


def _removed(world, admin, content_type, oid):
    world.act_as(admin)
    assert world.http.post(f"/moderation/admin/queue/{content_type}/{oid}/remove", json={"reason": "copyright"}).status_code == 200


def test_a_removed_deck_cannot_be_published_again_but_can_be_edited(world, admin):
    owner = _person(world, "ana")
    deck = _deck(world, owner, "Capitals")
    _removed(world, admin, "deck", deck)
    world.act_as(owner)
    refused = world.http.post(f"/public/decks/{deck}/publish", json={"language": "en"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "source_not_publishable", "reason": "removed_by_moderation"}
    settings = world.http.patch(f"/decks/{deck}/settings", json={"is_public": True})
    assert settings.status_code == 409 and settings.json()["detail"]["reason"] == "removed_by_moderation"
    side = world.http.patch(f"/decks/{deck}", json={"is_public": True})
    assert side.status_code == 409
    renamed = world.http.patch(f"/decks/{deck}", json={"name": "Renamed", "removal": None})
    assert renamed.status_code == 200, renamed.text
    stored = _doc(world, "decks", deck)
    assert stored["name"] == "Renamed" and stored["removal"]["reason"] == "copyright" and stored["is_public"] is False


def test_a_removed_book_cannot_be_published_again_but_can_be_edited(world, admin):
    owner = _person(world, "ana")
    book = _book(world, owner, "Notes")
    _removed(world, admin, "book", book)
    world.act_as(owner)
    refused = world.http.post(f"/public/books/{book}/publish", json={"language": "en"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "source_not_publishable", "reason": "removed_by_moderation"}
    side = world.http.put(f"/book/edit/{book}", json={"is_public": True})
    assert side.status_code == 409
    edited = world.http.put(f"/book/edit/{book}", json={"title": "Notes 2"})
    assert edited.status_code == 200, edited.text
    stored = _doc(world, "books", book)
    assert stored["title"] == "Notes 2" and stored["removal"] and stored["is_public"] is False


# ── 6. The email ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("locale", ["en", "es", "fr", "de", "ja"])
@pytest.mark.parametrize("is_copy", [False, True])
def test_the_email_renders_in_five_languages_without_a_button(locale, is_copy):
    from app.emails.render import KINDS, render_email
    from app.services.moderation import removal_email_values

    assert KINDS["content.removed"] == {"class": "account", "sender": "account", "button": "none"}
    values = removal_email_values(locale, "deck", "Capitals $1", "copyright", "See the notice", is_copy)
    filled = {"email": "ana@example.com", "support": "support@nowry.app", **values}
    rendered = render_email("content.removed", locale, filled, logo_url="https://x/logo.png")
    assert rendered.locale == locale
    assert "Capitals $1" in rendered.subject and "Capitals $1" in rendered.text
    assert "See the notice" in rendered.text and "See the notice" in rendered.html
    assert 'class="btn"' not in rendered.html and "leave" not in rendered.text.lower()
    bare = render_email("content.removed", locale, {**filled, **removal_email_values(locale, "book", "B", "spam", None, is_copy)},
                        logo_url="https://x/logo.png")
    assert "<p style=\"margin:0 0 14px;\"></p>" not in bare.html and "\n\n\n\n" not in bare.text


def test_the_email_values_name_the_reason_and_the_type_in_words():
    from app.services.moderation import removal_email_values

    own = removal_email_values("en", "deck", "Capitals", "spam", None, False)
    copy = removal_email_values("en", "book", "Notes", "copyright", "n", True)
    assert "deck" in own["lead"] and "Capitals" in own["lead"] and own["note_line"] == ""
    assert "copy" in copy["lead"] and "book" in copy["lead"] and copy["note_line"].endswith("n")
    assert "spam" in own["reason_line"].lower() and "copyright" in copy["reason_line"].lower()
    assert removal_email_values("pt", "deck", "C", "other", None, False) == removal_email_values("en", "deck", "C", "other", None, False)


def test_the_moderation_reads_are_indexed():
    from tests._fake_world import fake_world as world_of

    with world_of() as world:
        import app.config.database as database

        world.run(database.create_indexes())
        reports = world.run(world.raw["content_reports"].index_information())
        assert [tuple(k) for k in reports["content_reports_item"]["key"]] == [("content_type", 1), ("content_id", 1), ("status", 1)]
        for name in ("decks", "books"):
            info = world.run(world.raw[name].index_information())
            assert [tuple(k) for k in info["copies_by_source"]["key"]] == [("forked_from.id", 1)]
            assert [tuple(k) for k in info["removed_by_time"]["key"]] == [("removal.removed_at", 1)]
