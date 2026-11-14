"""
Remove the retired `role` field from stored accounts (ADMIN-004b, ADR-086 point 1, docs/prd-admin.md FR-009).

`role` grants nothing since ADR-086; `is_admin` is the only admin power. A dry
run by default: it prints how many accounts hold each role value, live and in
all. It stops (exit 1, nothing written) when any live account holds a role
other than "user", since that account was meant to be something the flag must
now say. `--apply` removes `role` from every account, deleted ones included.
No address, name or id is printed.

    railway run --environment development --service Nowry -- .venv/bin/python -I -m scripts.retire_role
    railway run --environment development --service Nowry -- .venv/bin/python -I -m scripts.retire_role --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional

#: The one role value that may be removed without a decision.
PLAIN_ROLE = "user"
#: Distinct role values read at most; far above any environment.
GROUP_LIMIT = 100
NO_ROLE = "(none)"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="scripts.retire_role", description="Remove the retired role field from accounts.")
    parser.add_argument("--apply", action="store_true", help="remove the field; without it nothing is written")
    return parser.parse_args(argv)


async def count_by_role(users: Any, live_only: bool) -> Dict[str, int]:
    """{role value: accounts}, with accounts that hold none under "(none)"."""
    pipeline: List[Dict[str, Any]] = [{"$match": {"deleted_at": None}}] if live_only else []
    pipeline.append({"$group": {"_id": "$role", "n": {"$sum": 1}}})
    groups = await users.aggregate(pipeline).to_list(length=GROUP_LIMIT)
    return {(NO_ROLE if group["_id"] is None else str(group["_id"])): group["n"] for group in groups}


def blocking(live: Dict[str, int]) -> int:
    """Live accounts holding a role other than "user"."""
    return sum(n for role, n in live.items() if role not in (PLAIN_ROLE, NO_ROLE))


def describe(label: str, counts: Dict[str, int]) -> str:
    parts = ", ".join(f"{role}: {counts[role]}" for role in sorted(counts)) or "none"
    return f"{label}: {parts}"


async def run(args: argparse.Namespace, users: Any = None) -> int:
    """Exit code 0 when shown or done, 1 when stopped."""
    if users is None:
        from app.config.database import users_collection

        users = users_collection
    live = await count_by_role(users, live_only=True)
    print(describe("live accounts by role", live))
    print(describe("all accounts by role", await count_by_role(users, live_only=False)))
    held = blocking(live)
    if held:
        print(f"stopped: {held} live account(s) hold a role other than \"{PLAIN_ROLE}\"; decide each with scripts.set_admin first; nothing written")
        return 1
    if not args.apply:
        print("dry run, nothing written")
        return 0
    result = await users.update_many({"role": {"$exists": True}}, {"$unset": {"role": ""}})
    print(f"role removed from {result.modified_count} account(s) (written)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
