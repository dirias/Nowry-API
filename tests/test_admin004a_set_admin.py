"""
ADMIN-004a — the grant script (ADR-086 point 3, docs/prd-admin.md FR-013).

Pins:

  1. Dry run by default: it prints the address and the flag before and after, and writes nothing.
  2. `--apply` sets the flag and writes one `admin_actions` row with actor "script".
  3. `--revoke --apply` clears it and logs `admin.revoke`.
  4. It refuses (exit 1, nothing written) unless exactly one active account has exactly that address,
     compared after lowercasing and trimming, never as a pattern.
  5. Nothing but the address and the flag is printed.
"""
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from scripts import set_admin
from tests._fake_collection import FakeCollection

ANA = ObjectId()


def users_with(*rows):
    users = FakeCollection()
    users.rows.extend(rows)
    return users


def account(**fields):
    return {"_id": ObjectId(), "email": "ana@example.com", "deleted_at": None, "username": "anita-k", "firebase_uid": "uid-ana", **fields}


async def run(capsys, users, log, *argv):
    code = await set_admin.run(set_admin.parse_args(list(argv)), users=users, log=log)
    return code, capsys.readouterr()


@pytest.mark.asyncio
async def test_a_dry_run_prints_the_change_and_writes_nothing(capsys):
    users, log = users_with(account(_id=ANA)), FakeCollection()
    code, out = await run(capsys, users, log, "--email", "  Ana@Example.com ")
    assert code == 0
    assert out.out.strip() == "ana@example.com  is_admin: false → true  (dry run, nothing written)"
    assert "is_admin" not in users.rows[0] and log.rows == []


@pytest.mark.asyncio
async def test_apply_sets_the_flag_and_logs_it(capsys):
    users, log = users_with(account(_id=ANA)), FakeCollection()
    code, out = await run(capsys, users, log, "--email", "ana@example.com", "--apply")
    assert code == 0 and out.out.strip() == "ana@example.com  is_admin: false → true  (written)"
    assert users.rows[0]["is_admin"] is True
    assert len(log.rows) == 1
    row = log.rows[0]
    assert (row["actor_id"], row["action"], row["target_type"], row["target_id"]) == ("script", "admin.grant", "user", str(ANA))
    assert row["details"] == {"before": False, "after": True}


@pytest.mark.asyncio
async def test_revoke_apply_clears_the_flag_and_logs_it(capsys):
    users, log = users_with(account(_id=ANA, is_admin=True)), FakeCollection()
    code, out = await run(capsys, users, log, "--email", "ana@example.com", "--revoke", "--apply")
    assert code == 0 and out.out.strip() == "ana@example.com  is_admin: true → false  (written)"
    assert "is_admin" not in users.rows[0]
    assert log.rows[0]["action"] == "admin.revoke"


@pytest.mark.asyncio
async def test_a_flag_already_as_asked_writes_nothing(capsys):
    users, log = users_with(account(_id=ANA, is_admin=True)), FakeCollection()
    code, out = await run(capsys, users, log, "--email", "ana@example.com", "--apply")
    assert code == 0 and out.out.strip() == "ana@example.com  is_admin: true → true  (no change, nothing written)"
    assert log.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [],
    [account(deleted_at=datetime(2026, 11, 1, tzinfo=timezone.utc))],
    [account(email="ana.b@example.com")],
    [account(), account()],
])
async def test_anything_but_exactly_one_active_account_is_refused(capsys, rows):
    users, log = users_with(*rows), FakeCollection()
    code, out = await run(capsys, users, log, "--email", "ana@example.com", "--apply")
    assert code == 1
    assert out.out.startswith("ana@example.com  refused:")
    assert all("is_admin" not in row for row in users.rows) and log.rows == []


@pytest.mark.asyncio
async def test_the_address_is_matched_exactly_not_as_a_pattern(capsys):
    users, log = users_with(account(email="anaXexample.com")), FakeCollection()
    code, _ = await run(capsys, users, log, "--email", "ana.example.com", "--apply")
    assert code == 1 and log.rows == []


@pytest.mark.asyncio
async def test_nothing_but_the_address_and_the_flag_is_printed(capsys):
    users, log = users_with(account(_id=ANA)), FakeCollection()
    _, out = await run(capsys, users, log, "--email", "ana@example.com", "--apply")
    for secret in (str(ANA), "uid-ana", "anita-k"):
        assert secret not in out.out + out.err


def test_the_email_is_required():
    with pytest.raises(SystemExit):
        set_admin.parse_args([])


def test_the_docstring_names_the_railway_command():
    assert "railway run --environment Production --service Nowry -- .venv/bin/python -m scripts.set_admin --email" in set_admin.__doc__
