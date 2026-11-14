"""
BETA-006 — the dev-test cohort code, refused by production (ADR-088, docs/prd-admin.md FR-014).

Pins:

  1. Off production, a code of cohort `dev` registers like any other and stamps cohort `dev`.
  2. With `ENV=production` every code of cohort `dev` is answered as unknown: the gate's
     refusal, the public check's `valid: false`; other cohorts are untouched.
  3. The refusal is logged naming the cohort and never the code.
"""
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.config import beta

CODES = "friends:c0-7f3a,dev:dev-test:50:2026-12-31"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODES", CODES)
    monkeypatch.setenv("BETA_INVITE_REQUIRED", "true")
    monkeypatch.delenv("BETA_CODE_MAX_USES", raising=False)
    monkeypatch.delenv("BETA_CODE_EXPIRES", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["development", "staging", ""])
async def test_off_production_the_dev_code_registers_and_stamps_dev(monkeypatch, environment):
    monkeypatch.setenv("ENV", environment)
    stamp = await beta.gate_new_account("DEV-TEST", AsyncMock(return_value=3), NOW)
    assert stamp["cohort"] == "dev" and stamp["invite_code"] == "dev-test"


@pytest.mark.asyncio
async def test_production_refuses_the_dev_code_as_unknown(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    count = AsyncMock(return_value=0)
    with pytest.raises(HTTPException) as refused:
        await beta.gate_new_account("dev-test", count, NOW)
    assert refused.value.status_code == 403 and refused.value.detail == {"code": beta.BETA_INVITE_REQUIRED_CODE}
    count.assert_not_awaited()
    assert beta.invite_cohort("dev-test") is None
    assert await beta.open_code_rule("dev-test", count, NOW) is None


@pytest.mark.asyncio
async def test_production_still_honours_the_other_cohorts(monkeypatch):
    monkeypatch.setenv("ENV", "Production")
    stamp = await beta.gate_new_account("c0-7f3a", AsyncMock(return_value=0), NOW)
    assert stamp["cohort"] == "friends"


def test_the_refusal_names_the_cohort_and_never_the_code(monkeypatch, caplog):
    monkeypatch.setenv("ENV", "production")
    with caplog.at_level(logging.WARNING):
        assert beta.code_rule("dev-test") is None
    assert "dev" in caplog.text and "dev-test" not in caplog.text


def test_the_start_up_lines_say_production_refuses_the_dev_cohort(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    lines = beta.describe_code_rules()
    assert any("'dev'" in line and "refused in production" in line for line in lines)
    assert not any("dev-test" in line for line in lines)
