"""POST /contact — the public contact form (SITE-006, docs/prd-public-site.md FR-7).

The route is a thin wrapper around `store_contact_message`, which is what is
tested here, so neither the rate limiter nor a Request is needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.models.Contact import ContactMessageCreate
from app.routers.contact import ACKNOWLEDGEMENT, contact_document, store_contact_message

VALID = {"name": "Ana", "email": "Ana@Example.com", "message": "The calendar lost my milestone.", "locale": "es", "page": "/contact"}


def test_valid_payload_is_accepted():
    payload = ContactMessageCreate(**VALID)
    assert payload.name == "Ana"
    assert payload.locale == "es"


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", ""),
        ("name", "x" * 81),
        ("email", "not-an-address"),
        ("message", "too short"),
        ("message", "x" * 4001),
        ("locale", "x"),
        ("page", "/" * 201),
    ],
)
def test_out_of_bounds_fields_are_rejected(field, value):
    with pytest.raises(ValidationError):
        ContactMessageCreate(**{**VALID, field: value})


def test_locale_and_page_are_optional():
    payload = ContactMessageCreate(name="Ana", email="ana@example.com", message="Ten chars..")
    assert payload.locale is None and payload.page is None


def test_document_normalises_and_stamps():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    doc = contact_document(ContactMessageCreate(**{**VALID, "name": "  Ana ", "message": "  Ten chars..  "}), now=now)
    assert doc == {
        "name": "Ana",
        "email": "ana@example.com",
        "message": "Ten chars..",
        "locale": "es",
        "page": "/contact",
        "status": "new",
        "created_at": now,
    }


@pytest.mark.asyncio
async def test_store_inserts_one_and_acknowledges():
    inserted = ObjectId()
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id=inserted))

    response = await store_contact_message(ContactMessageCreate(**VALID), collection)

    collection.insert_one.assert_awaited_once()
    stored = collection.insert_one.await_args.args[0]
    assert stored["status"] == "new" and stored["email"] == "ana@example.com"
    assert response.message_id == str(inserted)
    assert response.message == ACKNOWLEDGEMENT


# ---------------------------------------------------------------------------
# Through the router: validation, the 201, and the per-address limit.
# ---------------------------------------------------------------------------
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402

import app.routers.contact as contact_module  # noqa: E402
from app.core.limiter import limiter  # noqa: E402


def make_client(monkeypatch) -> tuple[TestClient, MagicMock]:
    """A bare app carrying only the contact router, the shared limiter, and a fake collection."""
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    monkeypatch.setattr(contact_module, "contact_messages_collection", collection)
    limiter.reset()
    api = FastAPI()
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.add_middleware(SlowAPIMiddleware)
    api.include_router(contact_module.router)
    return TestClient(api), collection


def test_route_stores_and_returns_201(monkeypatch):
    client, collection = make_client(monkeypatch)
    response = client.post("/contact", json=VALID)
    assert response.status_code == 201
    assert response.json()["message"] == ACKNOWLEDGEMENT
    collection.insert_one.assert_awaited_once()


def test_route_rejects_a_short_message_with_422(monkeypatch):
    client, collection = make_client(monkeypatch)
    response = client.post("/contact", json={**VALID, "message": "too short"})
    assert response.status_code == 422
    collection.insert_one.assert_not_awaited()


def test_route_limits_an_address_to_five_a_minute(monkeypatch):
    client, _ = make_client(monkeypatch)
    codes = [client.post("/contact", json=VALID).status_code for _ in range(6)]
    assert codes == [201, 201, 201, 201, 201, 429]
