"""The suite's XP lever (EVO-020): put a suite account just under a stage threshold.

The level-up scenario needs an account one review short of an evolution. The
helper sets `agent.xp` on a suite alias in the development database only, the
same guards as every other command there.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import e2e_dev


def _db(modified: int = 1) -> MagicMock:
    db = MagicMock()
    db.users.update_one = AsyncMock(return_value=MagicMock(modified_count=modified))
    return db


@pytest.mark.asyncio
async def test_sets_the_xp_of_the_live_suite_account():
    db = _db()
    result = await e2e_dev.set_xp(db, "rikydier+e2e-abc-levelup@gmail.com", 99)
    assert result == {"changed": 1, "xp": 99}
    query, update = db.users.update_one.await_args.args
    assert query["deleted_at"] is None
    assert query["email"]["$regex"] == "^rikydier\\+e2e\\-abc\\-levelup@gmail\\.com$"
    assert update == {"$set": {"agent.xp": 99}}


@pytest.mark.asyncio
async def test_refuses_a_negative_total():
    with pytest.raises(SystemExit):
        await e2e_dev.set_xp(_db(), "rikydier+e2e-abc-levelup@gmail.com", -1)


@pytest.mark.asyncio
async def test_is_reached_only_through_the_guarded_dispatch(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "Production")
    with pytest.raises(SystemExit):
        await e2e_dev.main(["set-xp", "rikydier+e2e-abc-levelup@gmail.com", "99"])


def test_refuses_an_address_that_is_not_a_suite_alias():
    with pytest.raises(SystemExit):
        e2e_dev.guard_alias("someone@example.com")
