"""
ADMIN-008a — the bug report's limits (docs/prd-admin.md FR-008).

Pins, each refusing with nothing stored:

  1. 413 `{"code": "too_large", "field": <name>}`: title over 140 characters; description,
     steps (and the two behaviour fields) over 4,000; more than 3 screenshots; a screenshot
     over 2 MB once decoded, not base64, or not PNG/JPEG/WebP by its first bytes.
  2. 413 with field "body" for a request over 7 MB, declared or streamed without a length.
  3. 429 `{"code": "daily_limit", "retry_at": <next UTC midnight>}` on an account's 11th report of
     the UTC day; yesterday's reports and other accounts' reports do not count.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.routers import bugs as bugs_router
from tests._bug_world import ADMIN, JPEG, PNG, TESTER, WEBP, World, data_url, report

MB = 1024 * 1024


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def refused(response, field: str) -> bool:
    return response.status_code == 413 and response.json()["detail"] == {"code": "too_large", "field": field}


@pytest.mark.parametrize("field, size", [
    ("title", 141), ("description", 4001), ("steps_to_reproduce", 4001),
    ("expected_behavior", 4001), ("actual_behavior", 4001),
])
def test_a_text_over_its_limit_is_413_with_its_name(world, field, size):
    assert refused(world.client.post("/bugs", json=report(**{field: "x" * size})), field)
    assert world.bugs() == []


def test_texts_at_their_limits_are_taken(world):
    body = report(title="x" * 140, description="x" * 4000, steps_to_reproduce="x" * 4000)
    assert world.client.post("/bugs", json=body).status_code == 200


def shot(raw: bytes, kind: str = "png") -> dict:
    return {"filename": f"s.{kind}", "data": data_url(raw, kind)}


def test_three_screenshots_of_each_kind_are_taken(world):
    shots = [shot(PNG), shot(JPEG, "jpeg"), shot(WEBP, "webp")]
    assert world.client.post("/bugs", json=report(screenshots=shots)).status_code == 200
    [stored] = world.bugs()
    assert [s["data"] for s in stored["screenshots"]] == [s["data"] for s in shots]


def test_a_raw_base64_screenshot_without_the_data_prefix_is_taken(world):
    raw = data_url(PNG).split(",", 1)[1]
    assert world.client.post("/bugs", json=report(screenshots=[{"filename": "a.png", "data": raw}])).status_code == 200


@pytest.mark.parametrize("shots", [
    [shot(PNG)] * 4,
    [shot(PNG + b"\x00" * (2 * MB))],
    [shot(b"GIF89a" + b"\x00" * 64, "gif")],
    [{"filename": "x.png", "data": "data:image/png;base64,@@not base64@@"}],
], ids=["four", "over-2-MB", "gif", "not-base64"])
def test_a_screenshot_outside_the_rules_is_413_screenshots(world, shots):
    assert refused(world.client.post("/bugs", json=report(screenshots=shots)), "screenshots")
    assert world.bugs() == []


def test_a_screenshot_of_exactly_2_mb_is_taken(world):
    raw = PNG + b"\x00" * (2 * MB - len(PNG))
    assert world.client.post("/bugs", json=report(screenshots=[shot(raw)])).status_code == 200


def oversized_body() -> bytes:
    return json.dumps(report(description="x" * (7 * MB + 1))).encode()


def test_a_body_over_7_mb_is_413_body(world):
    response = world.client.post("/bugs", content=oversized_body(), headers={"content-type": "application/json"})
    assert refused(response, "body") and world.bugs() == []


def test_a_streamed_body_without_a_length_is_cut_at_7_mb(world):
    payload = oversized_body()

    def chunks():
        for start in range(0, len(payload), MB):
            yield payload[start:start + MB]

    response = world.client.post("/bugs", content=chunks(), headers={"content-type": "application/json"})
    assert refused(response, "body") and world.bugs() == []


def test_a_streamed_body_under_the_limit_is_read(world):
    payload = json.dumps(report()).encode()
    response = world.client.post("/bugs", content=iter([payload[:10], payload[10:]]), headers={"content-type": "application/json"})
    assert response.status_code == 200


def seed_reports(world, who, count: int, created_at: datetime) -> None:
    rows = [{"user_id": str(who), "title": "t", "description": "d", "status": "new", "created_at": created_at} for _ in range(count)]
    world.run(world.database["bugs"].insert_many(rows))


def test_the_eleventh_report_of_the_utc_day_is_429_until_midnight(world):
    now = datetime.now(timezone.utc)
    seed_reports(world, TESTER, 10, now)
    response = world.client.post("/bugs", json=report())
    assert response.status_code == 429
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert response.json()["detail"] == {"code": "daily_limit", "retry_at": midnight.isoformat()}
    assert len(world.bugs()) == 10


def test_the_tenth_report_is_taken_and_older_or_other_reports_do_not_count(world):
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seed_reports(world, TESTER, 9, now)
    seed_reports(world, TESTER, 5, midnight - timedelta(seconds=1))
    seed_reports(world, ADMIN, 10, now)
    assert world.client.post("/bugs", json=report()).status_code == 200
    assert world.client.post("/bugs", json=report()).status_code == 429


def test_the_day_starts_at_utc_midnight():
    noon = datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc)
    assert bugs_router.utc_day_bounds(noon) == (
        datetime(2026, 11, 14, tzinfo=timezone.utc), datetime(2026, 11, 15, tzinfo=timezone.utc),
    )
