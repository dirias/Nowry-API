"""QA-018: the deck library and a deck's settings at the route level.

Both clients call ``GET /decks`` on every open; it was 19 % covered. These
tests run the real queries over the fake world: two users, decks and cards
created through the API's own create routes (so the documents have the shape
production writes), then the fields the clients read, the filters, the
settings routes, the daily budget, the soft delete and its cascade — and the
wrong-owner case for every id route, which must write nothing.
"""
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

NOW = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


@pytest.fixture
def world():
    with fake_world() as w:
        yield w


def _deck(world, name, **extra):
    response = world.http.post("/decks", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()["_id"]


def _card(world, deck_id, title, **fields):
    response = world.http.post("/study-cards", json={"title": title, "content": f"{title} answer", "deck_id": deck_id})
    assert response.status_code in (200, 201), response.text
    card_id = ObjectId(response.json().get("_id") or response.json().get("id"))
    if fields:
        world.run(world.raw["cards"].update_one({"_id": card_id}, {"$set": fields}))
    return card_id


def _read_deck(world, deck_id):
    return world.run(world.raw["decks"].find_one({"_id": ObjectId(deck_id)}))


# ── GET /decks ──────────────────────────────────────────────────────────────

def test_the_library_lists_only_the_callers_live_decks_with_their_counts(world):
    me, other = world.new_user("me"), world.new_user("other")
    world.act_as(other)
    _deck(world, "Theirs")
    world.act_as(me)
    deck = _deck(world, "Mine")
    _card(world, deck, "new one")
    _card(world, deck, "due", last_reviewed=NOW - timedelta(days=3), next_review=NOW - timedelta(days=1), repetitions=3, ease_factor=2.5)
    _card(world, deck, "later", last_reviewed=NOW - timedelta(days=1), next_review=NOW + timedelta(hours=5), repetitions=2, ease_factor=1.9)
    gone = _card(world, deck, "deleted")
    world.run(world.raw["cards"].update_one({"_id": gone}, {"$set": {"deleted_at": NOW}}))
    deleted_deck = _deck(world, "Gone")
    world.run(world.raw["decks"].update_one({"_id": ObjectId(deleted_deck)}, {"$set": {"deleted_at": NOW}}))

    listed = world.http.get("/decks").json()
    assert [d["name"] for d in listed] == ["Mine"]
    row = listed[0]
    assert (row["total_cards"], row["due_cards"], row["new_cards"], row["resting_cards"]) == (3, 1, 1, 0)
    assert row["is_due_soon"] is True and row["hours_until_due"] == 5
    assert row["mastery"] == round(((2.2 - 1.3) / 1.2) * 100)
    assert row["last_studied"] is not None


def test_type_and_archived_filters(world):
    me = world.new_user("me")
    world.act_as(me)
    flash = _deck(world, "Flash")
    legacy = _deck(world, "Legacy")
    world.run(world.raw["decks"].update_one({"_id": ObjectId(legacy)}, {"$unset": {"deck_type": ""}}))
    quiz = _deck(world, "Quiz", deck_type="quiz")
    archived = _deck(world, "Old")
    assert world.http.post(f"/decks/{archived}/archive").status_code == 200

    names = lambda response: sorted(d["name"] for d in response.json())
    assert names(world.http.get("/decks")) == ["Flash", "Legacy", "Quiz"]
    assert names(world.http.get("/decks", params={"type": "flashcard"})) == ["Flash", "Legacy"]
    assert names(world.http.get("/decks", params={"type": "quiz"})) == ["Quiz"]
    assert names(world.http.get("/decks", params={"archived": "true"})) == ["Old"]


def test_counts_are_capped_by_the_pace_and_what_was_studied_today(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Paced")
    world.run(world.raw["decks"].update_one({"_id": ObjectId(deck)}, {"$set": {"config": {"pace_mode": "relaxed", "new_per_day": 2, "max_reviews_per_day": 1}}}))
    for n in range(4):
        _card(world, deck, f"new {n}")
    for n in range(3):
        _card(world, deck, f"due {n}", last_reviewed=NOW - timedelta(days=2), next_review=NOW - timedelta(hours=1), repetitions=4, ease_factor=2.5)
    # One new card already studied today eats one of the two new slots.
    _card(world, deck, "studied today", last_reviewed=NOW, next_review=NOW + timedelta(days=1), repetitions=1, ease_factor=2.5)
    row = world.http.get("/decks").json()[0]
    assert (row["new_cards"], row["due_cards"]) == (1, 1)


# ── settings, config, budget ────────────────────────────────────────────────

def test_settings_publish_once_and_keep_forked_from(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Public one")
    world.run(world.raw["decks"].update_one({"_id": ObjectId(deck)}, {"$set": {"public_metadata": {"forked_from": "deck_origin"}}}))

    first = world.http.patch(f"/decks/{deck}/settings", json={
        "is_public": True,
        "public_metadata": {"description": "Capitals", "forked_from": "forged"},
        "config": {"pace_mode": "intensive"},
        "voice_settings": {"front": {"voice_lang": "fr-FR"}}
    })
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["is_public"] is True and body["published_at"] is not None
    assert body["config"]["pace_mode"] == "intensive"
    assert body["public_metadata"] == {"description": "Capitals"}
    stored = _read_deck(world, deck)
    assert stored["public_metadata"] == {"forked_from": "deck_origin", "description": "Capitals"}
    assert stored["voice_settings"]["front"]["voice_lang"] == "fr-FR"

    published_at = stored["published_at"]
    second = world.http.patch(f"/decks/{deck}/settings", json={"is_public": True})
    assert _read_deck(world, deck)["published_at"] == published_at
    assert second.json()["published_at"] is not None


def test_config_resolves_the_pace_defaults_and_estimates_completion(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Config")
    for n in range(25):
        _card(world, deck, f"c{n}")
    _card(world, deck, "seen", last_reviewed=NOW, next_review=NOW + timedelta(days=1), repetitions=1, ease_factor=2.5)

    defaults = world.http.patch(f"/decks/{deck}/config", json={"pace_mode": "relaxed"}).json()
    assert (defaults["new_per_day"], defaults["max_reviews_per_day"]) == (10, 50)
    assert (defaults["total_cards"], defaults["introduced_count"], defaults["introduced_pct"]) == (26, 1, 3.8)
    assert defaults["estimated_completion_days"] == 3
    explicit = world.http.patch(f"/decks/{deck}/config", json={"pace_mode": "balanced", "new_per_day": 5}).json()
    assert (explicit["new_per_day"], explicit["max_reviews_per_day"], explicit["estimated_completion_days"]) == (5, 100, 5)
    assert _read_deck(world, deck)["config"] == {"pace_mode": "balanced", "new_per_day": 5, "max_reviews_per_day": 100}


def test_the_daily_budget_refuses_a_bad_zone_and_honours_the_pace(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Budget")
    world.run(world.raw["decks"].update_one({"_id": ObjectId(deck)}, {"$set": {"config": {"pace_mode": "balanced", "new_per_day": 3, "max_reviews_per_day": 2}}}))
    for n in range(5):
        _card(world, deck, f"new {n}")
    for n in range(4):
        _card(world, deck, f"due {n}", last_reviewed=NOW - timedelta(days=2), next_review=NOW - timedelta(hours=1), repetitions=2, ease_factor=2.5)

    assert world.http.get(f"/decks/{deck}/daily-budget", params={"tz": "Mars/Olympus"}).status_code == 400
    budget = world.http.get(f"/decks/{deck}/daily-budget", params={"tz": "America/Los_Angeles"}).json()
    assert (budget["new_cards_today"], budget["reviews_today"], budget["total_today"], budget["budget_reached"]) == (3, 2, 5, True)
    assert (budget["new_per_day_limit"], budget["reviews_per_day_limit"]) == (3, 2)


# ── delete ──────────────────────────────────────────────────────────────────

def test_delete_soft_deletes_the_deck_and_its_cards_and_unpublishes(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Doomed")
    world.http.patch(f"/decks/{deck}/settings", json={"is_public": True})
    cards = [_card(world, deck, f"c{n}") for n in range(3)]

    assert world.http.delete(f"/decks/{deck}").status_code == 204
    stored = _read_deck(world, deck)
    assert stored["deleted_at"] is not None and stored["is_public"] is False
    for card_id in cards:
        assert world.run(world.raw["cards"].find_one({"_id": card_id}))["deleted_at"] is not None
    assert world.http.get("/decks").json() == []
    assert world.http.get(f"/decks/{deck}").status_code == 404


# ── another user's deck ─────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path, body", [
    ("get", "/decks/{id}", None),
    ("get", "/decks/{id}/cards", None),
    ("patch", "/decks/{id}", {"name": "Taken"}),
    ("patch", "/decks/{id}/settings", {"is_public": True}),
    ("patch", "/decks/{id}/config", {"pace_mode": "relaxed"}),
    ("get", "/decks/{id}/daily-budget", None),
    ("delete", "/decks/{id}", None),
    ("post", "/decks/{id}/archive", None),
    ("post", "/decks/{id}/rest", {"until": None}),
])
def test_another_users_deck_is_refused_and_untouched(world, method, path, body):
    victim, attacker = world.new_user("victim"), world.new_user("attacker")
    world.act_as(victim)
    deck = _deck(world, "SECRET-DECK")
    _card(world, deck, "SECRET-CARD")
    world.act_as(attacker)
    world.touched.clear()

    kwargs = {"json": body} if body is not None else {}
    response = getattr(world.http, method)(path.format(id=deck), **kwargs)
    assert response.status_code in (403, 404), response.text
    assert "SECRET-" not in response.text
    assert world.touched == []
    assert _read_deck(world, deck).get("deleted_at") is None


# ── the rest of the owner's routes: read, cards, rename, archive and restore, rest and wake ──

def test_the_owner_reads_renames_and_cannot_touch_immutable_fields(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Named")
    _card(world, deck, "one")
    _card(world, deck, "two")

    read = world.http.get(f"/decks/{deck}")
    assert read.status_code == 200 and read.json()["name"] == "Named"
    cards = world.http.get(f"/decks/{deck}/cards").json()
    assert cards["total"] == 2 and {c["title"] for c in cards["items"]} == {"one", "two"}
    assert all(c["deck_id"] == deck for c in cards["items"])

    renamed = world.http.patch(f"/decks/{deck}", json={"name": "Renamed", "user_id": "someone-else", "forked_from": "x", "archived_at": "2026-01-01T00:00:00"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed"
    stored = _read_deck(world, deck)
    assert stored["user_id"] == str(me) and stored.get("forked_from") is None and stored["archived_at"] is None


def test_archive_and_restore_keep_the_schedule(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Shelved")
    due = _card(world, deck, "due", last_reviewed=NOW - timedelta(days=2), next_review=NOW - timedelta(hours=1), repetitions=2, ease_factor=2.5)

    archived = world.http.post(f"/decks/{deck}/archive").json()
    assert archived["archived_at"] is not None and archived["status"] == "archived"
    assert world.http.get("/decks").json() == []
    restored = world.http.post(f"/decks/{deck}/restore").json()
    assert restored["archived_at"] is None and restored["status"] == "review"
    assert world.run(world.raw["cards"].find_one({"_id": due}))["next_review"] == NOW - timedelta(hours=1)
    assert world.http.get("/decks").json()[0]["due_cards"] == 1


def test_rest_takes_the_cards_out_of_play_and_wake_returns_them(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Tired")
    _card(world, deck, "due", last_reviewed=NOW - timedelta(days=2), next_review=NOW - timedelta(hours=1), repetitions=2, ease_factor=2.5)
    _card(world, deck, "new")

    rested = world.http.post(f"/decks/{deck}/rest", json={})
    assert rested.status_code == 200, rested.text
    row = world.http.get("/decks").json()[0]
    assert (row["due_cards"], row["new_cards"], row["resting_cards"]) == (0, 0, 2)

    woken = world.http.post(f"/decks/{deck}/wake")
    assert woken.status_code == 200, woken.text
    row = world.http.get("/decks").json()[0]
    assert row["resting_cards"] == 0 and row["new_cards"] == 1


def test_the_field_is_the_learners_when_set_and_the_default_when_cleared(world):
    me = world.new_user("me")
    world.act_as(me)
    deck = _deck(world, "Fielded")
    assert world.http.put(f"/decks/{deck}/field", json={"field": "science"}).json() == {"field": "science", "source": "learner"}
    assert world.http.get(f"/decks/{deck}/field").json() == {"field": "science", "source": "learner"}
    cleared = world.http.put(f"/decks/{deck}/field", json={"field": None}).json()
    assert cleared["source"] in ("default", "guess", "name") and "field" not in _read_deck(world, deck)
