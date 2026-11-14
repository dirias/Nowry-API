"""
Rewrite stored bug statuses to the fixed list (ADMIN-007b, docs/prd-admin.md FR-011).

Statuses are `new | in_progress | fixed | wont_fix`. Reports stored before that
hold open, in-progress, resolved or closed; the API already reads them under
their new names, and this script writes those names. A dry run by default: it
prints how many reports hold each old value and writes nothing. `--apply`
rewrites them. Prints counts only, never a title or an id.

    railway run --environment development --service Nowry -- .venv/bin/python -I -m scripts.bug_statuses
    railway run --environment development --service Nowry -- .venv/bin/python -I -m scripts.bug_statuses --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional

from app.models.Bug import LEGACY_STATUSES


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="scripts.bug_statuses", description="Rewrite old bug statuses to the fixed list.")
    parser.add_argument("--apply", action="store_true", help="rewrite them; without it nothing is written")
    return parser.parse_args(argv)


async def count_old(bugs: Any) -> Dict[str, int]:
    """{old value: reports holding it}."""
    return {old: await bugs.count_documents({"status": old}) for old in LEGACY_STATUSES}


async def rewrite(bugs: Any) -> int:
    """Rewrite every old value to its new name; the number of reports changed."""
    changed = 0
    for old, new in LEGACY_STATUSES.items():
        result = await bugs.update_many({"status": old}, {"$set": {"status": new}})
        changed += result.modified_count
    return changed


async def run(args: argparse.Namespace, bugs: Any = None) -> int:
    """Exit code 0: counts shown, and with --apply written."""
    if bugs is None:
        from app.config.database import bugs_collection

        bugs = bugs_collection
    for old, count in (await count_old(bugs)).items():
        print(f"{old} → {LEGACY_STATUSES[old]}: {count}")
    if not args.apply:
        print("dry run, nothing written")
        return 0
    print(f"{await rewrite(bugs)} report(s) rewritten (written)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
