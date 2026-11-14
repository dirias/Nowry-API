"""
BUG-001 — testers report a problem from the web and the phone (docs/prd-admin.md FR-007).

Pins:

  1. While the beta is on, any signed-in account reports; the answer is as before, with status `new`.
  2. With the beta off, `POST /bugs` is 403 `{"code": "reports_closed"}` and nothing is stored.
  3. The phone's report: platform android, device and app version, no url, no browser info, no screenshots.
  4. The phone's areas are categories; today's four stay valid; anything else is 422.
  5. The operator hears `[Nowry] Bug: <title>` with severity, category, platform, device, url and the
     reporter's username — never the address.
"""
import pytest

from tests._bug_world import TESTER, World, report


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def test_a_web_report_is_stored_and_answered_as_before(world):
    response = world.client.post("/bugs", json=report())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"bug_id", "message", "status", "created_at"} and body["status"] == "new"
    [stored] = world.bugs()
    assert stored["user_id"] == str(TESTER) and stored["status"] == "new" and stored["platform"] == "web"
    assert stored["browser_info"]["screen_resolution"] == "1440x900" and stored["notes"] == []


def test_reports_are_closed_when_the_beta_is_off(world, monkeypatch):
    monkeypatch.setenv("BETA_ACTIVE", "false")
    response = world.client.post("/bugs", json=report())
    assert response.status_code == 403 and response.json()["detail"] == {"code": "reports_closed"}
    assert world.bugs() == [] and world.notices == []


def test_the_phone_report_carries_device_and_version_and_nothing_else(world):
    phone = {
        "title": "Review froze", "description": "The card did not flip.", "steps_to_reproduce": "",
        "category": "study", "platform": "android", "app_version": "1.0.30 (30)", "device": "Pixel 8 · Android 15",
    }
    assert world.client.post("/bugs", json=phone).status_code == 200
    [stored] = world.bugs()
    assert (stored["platform"], stored["app_version"], stored["device"]) == ("android", "1.0.30 (30)", "Pixel 8 · Android 15")
    assert stored["url"] == "" and stored["browser_info"] is None and stored["screenshots"] == []
    assert stored["severity"] == "medium"


@pytest.mark.parametrize("category", ["study", "books", "planning", "focus", "other", "ui", "functionality", "performance"])
def test_every_area_is_a_category(world, category):
    assert world.client.post("/bugs", json=report(category=category)).status_code == 200


@pytest.mark.parametrize("field, value", [
    ("category", "billing"), ("platform", "ios"), ("severity", "urgent"),
    ("app_version", "x" * 41), ("device", "x" * 121),
])
def test_values_outside_the_lists_are_refused(world, field, value):
    assert world.client.post("/bugs", json=report(**{field: value})).status_code == 422
    assert world.bugs() == []


def test_the_operator_hears_of_the_report_without_the_address(world):
    world.client.post("/bugs", json=report(platform="android", device="Pixel 8", app_version="1.0.30"))
    [(subject, text)] = world.notices
    assert subject == "[Nowry] Bug: The calendar lost a milestone"
    for expected in ("high", "functionality", "android", "Pixel 8", "1.0.30", "/calendar", "tester_one"):
        assert expected in text
    assert "t@example.com" not in text
