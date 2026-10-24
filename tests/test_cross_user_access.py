"""QA-007: nobody reaches another user's content.

A victim creates a book, a deck with a card, a task, a task list and a whole
annual plan (focus area, goal, activity, milestone) through the API's own
create endpoints, so the documents have exactly the shape production writes.
Then another user calls every id-bearing route under books, decks, cards,
tasks, task lists, the annual plan and the public library with the victim's
ids, and three things must hold for each call:

1. no write's filter matched a victim document (recorded at the collection,
   so a write the fake database turns into a no-op still counts);
2. the response carries none of the victim's text;
3. it is refused, unless it is on the short list of routes that answer 2xx
   with the caller's own (empty) data.

The database is mongomock behind every Motor object the app imported
(``tests/_fake_world.py``), so the handlers run their real queries.
"""
import re

import pytest
from bson import ObjectId

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

AREAS = r"^/(book/|study-cards/|decks/|tasks/|task-lists/|annual-plan|public/)"
MARK = "SECRET-"

#: 2xx for another user's ids on purpose: each answers with the caller's own data.
ANSWERS_THE_CALLER = {
    "DELETE /study-cards/tags/{tag}/rest": "wakes the caller's own tag of that name",
    "GET /annual-plan/quarter-reports/{annual_plan_id}": "lists the caller's reports for that id: none",
    "GET /public/books/{book_id}": "a private book reads as null, as a missing one does",
    "GET /public/decks/{deck_id}": "a private deck reads as null, as a missing one does",
}


@pytest.fixture(scope="module")
def world():
    with fake_world() as w:
        victim, attacker = w.new_user("victim"), w.new_user("attacker")
        w.act_as(victim)

        def create(path, body):
            response = w.http.post(path, json=body)
            assert response.status_code in (200, 201), f"seeding {path}: {response.status_code} {response.text[:200]}"
            payload = response.json()
            return payload.get("_id") or payload.get("id")

        ids = {"book": create("/book/create", {"title": f"{MARK}BOOK", "content": f"{MARK}PAGES"})}
        ids["deck"] = create("/decks", {"name": f"{MARK}DECK"})
        ids["card"] = create("/study-cards", {"title": f"{MARK}CARD", "content": f"{MARK}ANSWER", "deck_id": ids["deck"]})
        ids["task"] = create("/tasks/", {"title": f"{MARK}TASK"})
        ids["list"] = create("/task-lists/", {"name": f"{MARK}LIST"})
        ids["plan"] = create("/annual-plan", {"year": 2026, "title": f"{MARK}PLAN"})
        ids["area"] = create("/annual-plan/focus-areas", {"annual_plan_id": ids["plan"], "name": f"{MARK}AREA", "order": 0})
        ids["goal"] = create("/annual-plan/goals", {"focus_area_id": ids["area"], "title": f"{MARK}GOAL"})
        ids["activity"] = create(f"/annual-plan/goals/{ids['goal']}/activities", {"title": f"{MARK}ACTIVITY"})
        ids["milestone"] = create(f"/annual-plan/goals/{ids['goal']}/milestones", {"title": f"{MARK}MILESTONE"})

        victim_docs = set()
        for name in w.run(w.raw.list_collection_names()):
            for doc in w.run(w.raw[name].find({}, {"_id": 1}).to_list(length=None)):
                if doc["_id"] != attacker:
                    victim_docs.add((name, doc["_id"]))

        yield {"w": w, "http": w.http, "ids": ids, "victim": victim, "attacker": attacker,
               "touched": w.touched, "victim_docs": victim_docs, "app": w.app}


def _path_for(path: str, ids: dict) -> str:
    by_prefix = [("/study-cards/", "card"), ("/decks/", "deck"), ("/tasks/", "task"), ("/task-lists/", "list"),
                 ("/annual-plan/focus-areas/", "area"), ("/annual-plan/goals/", "goal"),
                 ("/annual-plan/activities/", "activity"), ("/annual-plan/", "plan")]
    named = {"book_id": "book", "deck_id": "deck", "goal_id": "goal", "milestone_id": "milestone", "annual_plan_id": "plan"}

    def fill(match):
        param = match.group(1)
        if param == "tag":
            return "victim-tag"
        if param in named:
            return ids[named[param]]
        key = next((key for prefix, key in by_prefix if path.startswith(prefix)), None)
        return ids[key] if key else str(ObjectId())  # an id-bearing route with no seeded document

    return re.sub(r"\{([^}]+)\}", fill, path)


def _targets(app) -> list:
    return [(method.upper(), path) for path, methods in app.openapi()["paths"].items() for method in methods
            if "{" in path and re.match(AREAS, path)]


def test_the_victim_reaches_their_own_content(world):
    """The control: without it, a sweep where every id was wrong would pass."""
    world["w"].act_as(world["victim"])
    ids = world["ids"]
    for path in (f"/book/{ids['book']}", f"/decks/{ids['deck']}", f"/study-cards/{ids['card']}",
                 f"/tasks/{ids['task']}", f"/annual-plan/goals/{ids['goal']}"):
        response = world["http"].get(path)
        assert response.status_code == 200 and MARK in response.text, path


def test_another_user_reaches_none_of_it(world):
    app, http, ids = world["app"], world["http"], world["ids"]
    targets = _targets(app)
    assert len(targets) >= 60, "the sweep lost most of its routes"

    world["w"].act_as(world["attacker"])
    body = {"title": "overwritten", "name": "overwritten", "content": "overwritten",
            "is_completed": True, "is_public": True, "completed": True}
    writes, leaks, answered = [], [], set()
    for method, path in targets:
        del world["touched"][:]
        response = http.request(method, _path_for(path, ids), json=body)
        hit = sorted({f"{name}" for name, _id in world["touched"] if (name, _id) in world["victim_docs"]})
        if hit:
            writes.append(f"{method} {path} wrote {hit}")
        if MARK in response.text:
            leaks.append(f"{method} {path}")
        if 200 <= response.status_code < 300:
            answered.add(f"{method} {path}")

    assert writes == []
    assert leaks == []
    assert sorted(answered - set(ANSWERS_THE_CALLER)) == []
    assert sorted(set(ANSWERS_THE_CALLER) - answered) == [], "an allowlisted route now refuses; drop it from the list"
