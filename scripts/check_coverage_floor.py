"""Fail when the API's line coverage drops below the floor (QA-025, ADR-049 amended).

The floor is the figure measured on the day it was written, and it only ever
rises: raise it here when a QA task lifts the package. Reads the JSON report
pytest-cov writes (`--cov-report=json:coverage.json`).

    python scripts/check_coverage_floor.py coverage.json
"""
import json
import sys

FLOOR_PERCENT = 74.0


def main(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        totals = json.load(handle)["totals"]
    covered = float(totals["percent_covered"])
    print(f"lines covered: {covered:.1f} % (floor {FLOOR_PERCENT:.0f} %)")
    if covered < FLOOR_PERCENT:
        print(f"coverage fell below the floor by {FLOOR_PERCENT - covered:.1f} points", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json"))
