"""QA-017: every Stripe event walks from a signed payload to the user's plan.

The six existing Stripe tests call the handlers; none posts a signed payload to
the route, so the dispatcher, the idempotency write and the signature check
were never exercised together. This file does, over the fake world, so the
plan field a paying user sees is what each event leaves behind.

Checkout is here too: the one route that starts a payment refuses an unknown
price before Stripe is called, creates the customer on demand, and refuses
while the beta keeps upgrades closed.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bson import ObjectId
from stripe import WebhookSignature

pytest.importorskip("mongomock_motor")

from tests._fake_world import fake_world  # noqa: E402

SECRET = "whsec_test_route_matrix"
PRICES = {"plus_monthly": "price_plus_m", "plus_annual": "price_plus_y",
          "pro_monthly": "price_pro_m", "pro_annual": "price_pro_y"}


@pytest.fixture
def world():
    with fake_world() as w, \
            patch("app.routers.stripe_webhooks.WEBHOOK_SECRET", SECRET), \
            patch("app.routers.stripe_webhooks.STRIPE_PRICE_IDS", PRICES):
        yield w


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    ts = int(time.time())
    signature = WebhookSignature._compute_signature(f"{ts}.{body.decode()}", SECRET)
    return body, {"stripe-signature": f"t={ts},v1={signature}", "content-type": "application/json"}


def _event(kind: str, obj: dict, event_id: str = None) -> dict:
    return {"id": event_id or f"evt_{ObjectId()}", "object": "event", "type": kind, "data": {"object": obj}}


def _subscription(customer: str, price: str, status: str = "active", interval: str = "month") -> dict:
    return {"id": "sub_1", "customer": customer, "status": status,
            "items": {"data": [{"price": {"id": price}, "plan": {"interval": interval}}]},
            "current_period_end": int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())}


def _post(world, payload: dict):
    body, headers = _signed(payload)
    return world.http.post("/stripe/webhook", content=body, headers=headers)


def _user(world, **fields):
    uid = world.new_user("payer", stripe_customer_id="cus_payer", **fields)
    return uid, lambda: world.run(world.raw["users"].find_one({"_id": uid}))


# ── the signature and idempotency gate ──────────────────────────────────────

def test_a_bad_signature_is_refused_and_nothing_is_written(world):
    uid, read = _user(world)
    body = json.dumps(_event("invoice.payment_failed", {"customer": "cus_payer"})).encode()
    response = world.http.post("/stripe/webhook", content=body,
                               headers={"stripe-signature": "t=1,v1=nope", "content-type": "application/json"})
    assert response.status_code == 400
    assert read()["subscription"]["status"] == "active"
    assert world.run(world.raw["stripe_processed_events"].count_documents({})) == 0


def test_a_replayed_event_is_a_no_op(world):
    uid, read = _user(world)
    event = _event("invoice.payment_failed", {"customer": "cus_payer"}, event_id="evt_once")
    assert _post(world, event).json() == {"status": "ok"}
    first = read()["subscription"]["subscription_status_updated_at"]
    world.touched.clear()
    assert _post(world, event).json() == {"status": "already_processed"}
    assert read()["subscription"]["subscription_status_updated_at"] == first
    assert world.touched == []
    assert world.run(world.raw["stripe_processed_events"].count_documents({"stripe_event_id": "evt_once"})) == 1


def test_an_unknown_event_type_is_acknowledged_and_ignored(world):
    uid, read = _user(world)
    assert _post(world, _event("charge.refunded", {"customer": "cus_payer"})).status_code == 200
    assert read()["subscription"] == {"tier": "pro", "status": "active"}


# ── subscription created / updated: the price maps to the tier ──────────────

@pytest.mark.parametrize("price, interval, tier", [
    ("price_plus_m", "month", "plus"), ("price_plus_y", "year", "plus"),
    ("price_pro_m", "month", "pro"), ("price_pro_y", "year", "pro"),
])
def test_a_known_price_sets_the_tier_and_interval(world, price, interval, tier):
    uid, read = _user(world, subscription={"tier": "free", "status": "active"})
    kind = "customer.subscription.created" if tier == "plus" else "customer.subscription.updated"
    assert _post(world, _event(kind, _subscription("cus_payer", price, interval=interval))).status_code == 200
    sub = read()["subscription"]
    assert (sub["tier"], sub["status"], sub["billing_interval"], sub["stripe_subscription_id"]) == (tier, "active", interval, "sub_1")
    assert sub["next_billing_date"] > datetime.now()


def test_an_unknown_price_cannot_escalate_the_tier(world):
    uid, read = _user(world, subscription={"tier": "free", "status": "active"})
    _post(world, _event("customer.subscription.updated", _subscription("cus_payer", "price_forged")))
    assert read()["subscription"]["tier"] == "free"


def test_a_past_due_subscription_keeps_its_tier_with_the_status(world):
    uid, read = _user(world)
    _post(world, _event("customer.subscription.updated", _subscription("cus_payer", "price_pro_m", status="past_due")))
    sub = read()["subscription"]
    assert (sub["tier"], sub["status"]) == ("pro", "past_due")


def test_an_event_for_an_unknown_customer_touches_nobody(world):
    uid, read = _user(world)
    world.touched.clear()
    _post(world, _event("customer.subscription.updated", _subscription("cus_stranger", "price_plus_m")))
    assert world.touched == []
    assert read()["subscription"]["tier"] == "pro"


# ── payment failed, then deleted: the grace period ──────────────────────────

def test_payment_failed_starts_the_grace_clock(world):
    uid, read = _user(world)
    _post(world, _event("invoice.payment_failed", {"customer": "cus_payer"}))
    sub = read()["subscription"]
    assert sub["status"] == "past_due" and sub["tier"] == "pro"
    assert abs(datetime.utcnow() - sub["subscription_status_updated_at"].replace(tzinfo=None)) < timedelta(minutes=1)


def test_a_direct_cancellation_downgrades_at_once(world):
    uid, read = _user(world, subscription={"tier": "pro", "status": "active", "stripe_subscription_id": "sub_1"})
    _post(world, _event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_payer"}))
    sub = read()["subscription"]
    assert (sub["tier"], sub["status"], sub["stripe_subscription_id"], sub["next_billing_date"]) == ("free", "active", None, None)


def test_a_deletion_inside_the_grace_period_keeps_the_tier(world):
    uid, read = _user(world, subscription={"tier": "pro", "status": "past_due",
                                           "subscription_status_updated_at": datetime.now(timezone.utc) - timedelta(days=3)})
    _post(world, _event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_payer"}))
    assert read()["subscription"]["tier"] == "pro"


def test_a_deletion_after_the_grace_period_downgrades(world):
    uid, read = _user(world, subscription={"tier": "pro", "status": "past_due",
                                           "subscription_status_updated_at": datetime.now(timezone.utc) - timedelta(days=8)})
    _post(world, _event("customer.subscription.deleted", {"id": "sub_1", "customer": "cus_payer"}))
    assert read()["subscription"]["tier"] == "free"


def test_payment_succeeded_resets_usage_and_clears_past_due(world):
    period_end = int((datetime.now(timezone.utc) + timedelta(days=31)).timestamp())
    uid, read = _user(world, subscription={"tier": "plus", "status": "past_due", "ai_usage_count": 37})
    _post(world, _event("invoice.payment_succeeded",
                        {"customer": "cus_payer", "lines": {"data": [{"period": {"end": period_end}}]}}))
    sub = read()["subscription"]
    assert (sub["tier"], sub["status"], sub["ai_usage_count"]) == ("plus", "active", 0)
    assert sub["ai_usage_reset_date"] == datetime.fromtimestamp(period_end, tz=timezone.utc).replace(tzinfo=None)


# ── checkout ────────────────────────────────────────────────────────────────

@pytest.fixture
def checkout(world):
    uid = world.new_user("buyer")
    world.act_as(uid)
    created = {}

    def create_customer(**kwargs):
        created["customer"] = kwargs
        return SimpleNamespace(id="cus_new")

    def create_session(**kwargs):
        created["session"] = kwargs
        return SimpleNamespace(url="https://checkout.stripe.test/s_1")

    with patch("app.routers.subscriptions.VALID_PRICE_IDS", set(PRICES.values())), \
            patch("app.routers.subscriptions.require_upgrades_open", lambda: None), \
            patch("app.routers.subscriptions.stripe.Customer.create", side_effect=create_customer), \
            patch("app.routers.subscriptions.stripe.checkout.Session.create", side_effect=create_session):
        yield SimpleNamespace(world=world, uid=uid, created=created)


def test_an_unknown_price_is_refused_before_stripe_is_called(checkout):
    response = checkout.world.http.post("/stripe/create-checkout-session", json={"price_id": "price_forged"})
    assert response.status_code == 400
    assert checkout.created == {}


def test_a_known_price_creates_the_customer_on_demand_and_returns_the_session(checkout):
    response = checkout.world.http.post("/stripe/create-checkout-session", json={"price_id": "price_pro_y"})
    assert response.status_code == 200, response.text
    assert response.json() == {"url": "https://checkout.stripe.test/s_1"}
    assert checkout.created["customer"]["email"] == "buyer@example.com"
    assert checkout.created["customer"]["metadata"]["user_id"] == str(checkout.uid)
    user = checkout.world.run(checkout.world.raw["users"].find_one({"_id": checkout.uid}))
    assert user["stripe_customer_id"] == "cus_new"
    session = checkout.created["session"]
    assert session["customer"] == "cus_new" and session["mode"] == "subscription"
    assert session["line_items"] == [{"price": "price_pro_y", "quantity": 1}]


def test_an_existing_customer_is_reused(checkout):
    checkout.world.run(checkout.world.raw["users"].update_one({"_id": checkout.uid}, {"$set": {"stripe_customer_id": "cus_old"}}))
    checkout.world.http.post("/stripe/create-checkout-session", json={"price_id": "price_plus_m"})
    assert "customer" not in checkout.created
    assert checkout.created["session"]["customer"] == "cus_old"


def test_closed_upgrades_refuse_before_any_stripe_call(world):
    from fastapi import HTTPException
    uid = world.new_user("buyer")
    world.act_as(uid)

    def closed():
        raise HTTPException(status_code=403, detail={"code": "upgrades_closed"})

    with patch("app.routers.subscriptions.VALID_PRICE_IDS", set(PRICES.values())), \
            patch("app.routers.subscriptions.require_upgrades_open", closed), \
            patch("app.routers.subscriptions.stripe.checkout.Session.create") as create:
        response = world.http.post("/stripe/create-checkout-session", json={"price_id": "price_plus_m"})
    assert response.status_code == 403
    create.assert_not_called()


def test_a_caller_without_a_user_document_is_404(world):
    uid = world.new_user("ghost")
    world.act_as(uid)
    world.run(world.raw["users"].delete_one({"_id": uid}))
    with patch("app.routers.subscriptions.VALID_PRICE_IDS", set(PRICES.values())), \
            patch("app.routers.subscriptions.require_upgrades_open", lambda: None), \
            patch("app.routers.subscriptions.stripe.checkout.Session.create") as create:
        response = world.http.post("/stripe/create-checkout-session", json={"price_id": "price_plus_m"})
    assert response.status_code == 404
    create.assert_not_called()
