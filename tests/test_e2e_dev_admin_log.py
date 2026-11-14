"""The suite's view of the admin log (E2E-ADMIN-1): which admin actions touched a suite address.

The admin scenarios check that every invitation write leaves a row (FR-003). The
helper reads the action names only, for suite aliases, in development only; the
run's cleanup removes those rows with the rest of the run.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import e2e_dev


def _db(rows):
    db = MagicMock()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=rows)
    db.admin_actions.find = MagicMock(return_value=cursor)
    return db, cursor


@pytest.mark.asyncio
async def test_lists_the_actions_about_the_address_oldest_first():
    db, cursor = _db([{"action": "invite.send"}, {"action": "invite.resend"}, {"action": "invite.revoke"}])
    result = await e2e_dev.admin_log(db, "rikydier+e2e-abc-adm-a@gmail.com")
    assert result == {"actions": ["invite.send", "invite.resend", "invite.revoke"]}
    (query,) = db.admin_actions.find.call_args.args
    assert query["target_title"]["$regex"] == "^rikydier\\+e2e\\-abc\\-adm\\-a@gmail\\.com$"
    cursor.sort.assert_called_once_with("created_at", 1)


@pytest.mark.asyncio
async def test_is_reached_only_through_the_guarded_dispatch(monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "Production")
    with pytest.raises(SystemExit):
        await e2e_dev.main(["admin-log", "rikydier+e2e-abc-adm-a@gmail.com"])


def test_cleanup_reaches_the_admin_log():
    import inspect
    assert '("admin_actions", "target_title")' in inspect.getsource(e2e_dev.cleanup)
