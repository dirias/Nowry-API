"""QA-022: index creation runs under test.

``create_indexes`` ran at 1 %: the due-card indexes from QA-003, the unique
keys that make sign-up safe and the TTLs that keep quiz sessions from piling
up were declared in a function no test ever called, so a typo there would ship
as a slow query or a failed startup with every suite green. This runs it
against the fake world's database and reads the indexes back by name.
"""
import pytest

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402


@pytest.fixture(scope="module")
def indexes():
    with fake_world() as world:
        import app.config.database as database

        world.run(database.create_indexes())
        names = {}
        for name in world.run(world.raw.list_collection_names()):
            names[name] = world.run(world.raw[name].index_information())
        yield {"by": names, "database": database}


def _keys(info):
    return {name: [tuple(k) for k in spec["key"]] for name, spec in info.items()}


def test_the_user_identity_fields_are_unique(indexes):
    users = indexes["by"]["users"]
    for field in ("firebase_uid", "email", "username"):
        spec = users[f"{field}_1"]
        assert spec["key"] == [(field, 1)] and spec.get("unique") is True, field


def test_the_due_card_indexes_exist_on_the_fields_the_queue_reads(indexes):
    database = indexes["database"]
    cards = indexes["by"]["cards"]
    assert [tuple(k) for k in cards[database.DUE_BY_USER_INDEX]["key"]] == [("user_id", 1), ("deleted_at", 1), ("next_review", 1)]
    assert [tuple(k) for k in cards[database.DUE_BY_DECK_INDEX]["key"]] == [("deck_id", 1), ("deleted_at", 1), ("next_review", 1)]
    assert database.UNUSED_REVIEW_DATE_INDEX not in cards
    assert [tuple(k) for k in cards["user_id_1_marked_at_-1"]["key"]] == [("user_id", 1), ("marked_at", -1)]


def test_the_owner_reads_are_indexed(indexes):
    by = indexes["by"]
    assert "user_id_1" in by["decks"] and "user_id_1" in by["books"] and "user_id_1" in by["tasks"]
    assert by["annual_plans"]["user_id_1_year_1"].get("unique") is True
    assert _keys(by["blocks"])["blocks_owner"] == [("user_id", 1), ("deleted_at", 1)]
    assert by["block_log"]["block_log_key"].get("unique") is True
    assert _keys(by["task_lists"])["task_lists_owner"] == [("user_id", 1), ("deleted_at", 1)]
    assert by["daily_routines"]["user_id_1"].get("unique") is True


def test_quiz_sessions_expire_and_are_read_by_owner(indexes):
    quiz = indexes["by"]["quiz_sessions"]
    assert quiz["quiz_sessions_ttl"].get("expireAfterSeconds") == 3600
    assert quiz["session_id_1"].get("unique") is True
    assert _keys(quiz)["quiz_sessions_ownership"] == [("session_id", 1), ("user_id", 1)]
    assert indexes["by"]["ai_quiz_sessions"]["ai_quiz_sessions_ttl"].get("expireAfterSeconds") == 86400


def test_the_session_log_and_the_attempt_ledger_are_indexed(indexes):
    sessions = indexes["by"]["study_sessions"]
    client = sessions["study_sessions_client_session"]
    assert client.get("unique") is True
    assert client.get("partialFilterExpression") == {"client_session_id": {"$type": "string"}}
    assert _keys(sessions)["study_sessions_user_history"] == [("user_id", 1), ("completed_at", -1)]
    attempts = indexes["by"]["card_attempts"]
    assert _keys(attempts)["card_attempts_session"] == [("user_id", 1), ("client_session_id", 1), ("graded_at", 1)]
    assert _keys(attempts)["card_attempts_card"] == [("user_id", 1), ("card_id", 1), ("graded_at", -1)]


def test_the_media_ledger_is_read_by_owner_and_date_and_by_book(indexes):
    media = _keys(indexes["by"]["media"])
    assert media["media_owner_date"] == [("user_id", 1), ("created_at", -1)]
    assert media["media_book"] == [("book_id", 1)]
