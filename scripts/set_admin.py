"""
Grant or take away admin power (ADMIN-004a, ADR-086 point 3, docs/prd-admin.md FR-013).

Admin power is the `is_admin` flag on one account, and this script is the only
way it is set. A dry run by default: it prints the address and the flag before
and after, and writes nothing. `--apply` writes the flag and one row in the
admin log (`actor_id: "script"`). It refuses, writing nothing, unless exactly
one active account has exactly that address (lowercased; never a pattern).
Nothing but the address and the flag is printed.

    railway run --environment Production --service Nowry -- .venv/bin/python -I -m scripts.set_admin --email <address>
    railway run --environment Production --service Nowry -- .venv/bin/python -I -m scripts.set_admin --email <address> --apply
    railway run --environment Production --service Nowry -- .venv/bin/python -I -m scripts.set_admin --email <address> --revoke --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional

from app.services.admin_actions import record_admin_action


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="scripts.set_admin", description="Grant or revoke admin power.")
    parser.add_argument("--email", required=True, help="the account's address, exactly")
    parser.add_argument("--revoke", action="store_true", help="take admin power away instead of granting it")
    parser.add_argument("--apply", action="store_true", help="write the change; without it nothing is written")
    return parser.parse_args(argv)


def _flag(value: bool) -> str:
    return "true" if value else "false"


def describe(address: str, before: bool, after: bool, outcome: str) -> str:
    """The one line printed: the address and the flag before and after."""
    return f"{address}  is_admin: {_flag(before)} → {_flag(after)}  ({outcome})"


async def find_active_accounts(users: Any, address: str) -> List[Dict[str, Any]]:
    """Active accounts with exactly this address; two are enough to refuse."""
    return await users.find({"email": address, "deleted_at": None}, {"is_admin": 1}).to_list(length=2)


async def write_flag(users: Any, log: Any, account: Dict[str, Any], address: str, before: bool, after: bool) -> None:
    """Set or clear the flag, then leave the admin log row."""
    change = {"$set": {"is_admin": True}} if after else {"$unset": {"is_admin": ""}}
    await users.update_one({"_id": account["_id"], "deleted_at": None}, change)
    await record_admin_action(
        "script", "admin.grant" if after else "admin.revoke", "user", str(account["_id"]), address,
        details={"before": before, "after": after}, collection=log,
    )


async def run(args: argparse.Namespace, users: Any = None, log: Any = None) -> int:
    """Exit code 0 when done or shown, 1 when refused."""
    if users is None or log is None:
        from app.config.database import admin_actions_collection, users_collection

        users = users_collection if users is None else users
        log = admin_actions_collection if log is None else log
    address = args.email.strip().lower()
    accounts = await find_active_accounts(users, address)
    if len(accounts) != 1:
        reason = "no active account has exactly this address" if not accounts else "more than one active account has this address"
        print(f"{address}  refused: {reason}; nothing written")
        return 1
    before = accounts[0].get("is_admin") is True
    after = not args.revoke
    if before == after:
        print(describe(address, before, after, "no change, nothing written"))
        return 0
    if not args.apply:
        print(describe(address, before, after, "dry run, nothing written"))
        return 0
    await write_flag(users, log, accounts[0], address, before, after)
    print(describe(address, before, after, "written"))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
