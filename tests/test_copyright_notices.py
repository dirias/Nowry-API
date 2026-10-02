"""POST /copyright-notices — the takedown form (GTM-008)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.models.CopyrightNotice import CopyrightNoticeCreate
from app.routers.copyright import ACKNOWLEDGEMENT, notice_document, store_copyright_notice

VALID = {
    "name": "Ana Rights",
    "email": "Ana@Example.com",
    "work": "My textbook 'Cells and Tissues', chapter 3.",
    "location": "https://nowry.app/public/deck/70b8d295f1d2c17f4e4b5678",
    "statement": True,
    "signature": "Ana Rights",
    "locale": "es",
}


def test_valid_notice_is_accepted():
    assert CopyrightNoticeCreate(**VALID).signature == "Ana Rights"


def test_the_statement_must_be_affirmed():
    with pytest.raises(ValidationError):
        CopyrightNoticeCreate(**{**VALID, "statement": False})


@pytest.mark.parametrize("field, value", [("name", ""), ("email", "nope"), ("work", "short"), ("location", "x"), ("signature", "x" * 121)])
def test_out_of_bounds_fields_are_rejected(field, value):
    with pytest.raises(ValidationError):
        CopyrightNoticeCreate(**{**VALID, field: value})


def test_document_lowercases_email_and_stamps():
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    doc = notice_document(CopyrightNoticeCreate(**VALID), now=now)
    assert doc["email"] == "ana@example.com" and doc["status"] == "new" and doc["created_at"] == now


@pytest.mark.asyncio
async def test_store_inserts_and_acknowledges():
    collection = MagicMock()
    collection.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId("70b8d295f1d2c17f4e4b5678")))
    response = await store_copyright_notice(CopyrightNoticeCreate(**VALID), collection)
    assert response.notice_id == "70b8d295f1d2c17f4e4b5678" and response.message == ACKNOWLEDGEMENT
    assert collection.insert_one.call_args[0][0]["work"].startswith("My textbook")
