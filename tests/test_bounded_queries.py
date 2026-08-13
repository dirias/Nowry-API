"""
SEC-03: Static regression guard — bounded-query audit.

RESEARCH.md's SEC-03 audit confirmed all `.find(` sites in the five
in-scope routers (cards.py, decks.py, study_cards.py, study_sessions.py,
users.py) are already bounded via `.to_list(length=N)` and/or `.limit(N)`.
This test does not "fix" anything — it converts that point-in-time manual
audit into a permanent regression guard that fails CI if a future edit
introduces an unbounded query in any of the five routers. `users.py` was
added in Phase 33 (PERF-01) after its streak calculation's unbounded
`async for` cursor iteration was fixed — this guard now covers it going
forward, closing the coverage gap that let that pattern exist unflagged.

Approach: read each router's source as text (no imports, no live app/DB
required) and, for every `.find(` call site (excluding `.find_one(`), assert
a bound token (`.to_list(length=` or `.limit(`) appears within a small
forward window of lines. The window is per-site so a genuinely unbounded
future `.find(` is still caught even if some other line in the file has a
`to_list`/`limit` elsewhere.
"""
from __future__ import annotations

import re
from pathlib import Path

ROUTERS_DIR = Path(__file__).resolve().parent.parent / "app" / "routers"

IN_SCOPE_FILES = [
    "cards.py",
    "decks.py",
    "study_cards.py",
    "study_sessions.py",
    "users.py",
]

# Number of lines to look ahead (inclusive of the match line) when searching
# for a bound token after a `.find(` call site.
FORWARD_WINDOW = 12

# Matches `.find(` but not `.find_one(`. Uses a negative lookbehind on "_one"
# immediately preceding the opening paren token.
FIND_CALL_RE = re.compile(r"\.find\(")
FIND_ONE_RE = re.compile(r"\.find_one\(")
BOUND_RE = re.compile(r"\.to_list\(\s*length\s*=|\.limit\(")


def _read_lines(filename: str) -> list[str]:
    path = ROUTERS_DIR / filename
    assert path.is_file(), f"Expected router source file not found: {path}"
    return path.read_text().splitlines()


def _find_call_sites(lines: list[str], filename: str) -> list[int]:
    """Return 0-indexed line numbers containing a `.find(` call, excluding `.find_one(`."""
    sites: list[int] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # Strip out find_one( occurrences before checking for find( so that a
        # line with both (or only find_one) doesn't produce a false positive.
        line_without_find_one = FIND_ONE_RE.sub("", line)
        if FIND_CALL_RE.search(line_without_find_one):
            sites.append(i)
    return sites


def _assert_bounded_within_window(lines: list[str], site_line: int, filename: str) -> None:
    window_end = min(len(lines), site_line + FORWARD_WINDOW)
    window_text = "\n".join(lines[site_line:window_end])
    assert BOUND_RE.search(window_text), (
        f"Unbounded `.find(` detected: {filename}:{site_line + 1} — no `.to_list(length=` "
        f"or `.limit(` found within {FORWARD_WINDOW} lines. SEC-03 requires every `.find(` "
        f"in the in-scope routers to be bounded."
    )


def test_all_find_sites_are_bounded():
    """Every `.find(` site (excluding `.find_one(`) in the five in-scope routers
    must be followed by a `.to_list(length=...)` or `.limit(...)` bound within a
    small forward window."""
    total_sites_checked = 0
    for filename in IN_SCOPE_FILES:
        lines = _read_lines(filename)
        sites = _find_call_sites(lines, filename)
        for site_line in sites:
            _assert_bounded_within_window(lines, site_line, filename)
            total_sites_checked += 1

    # Sanity check: the audit found sites; if this drops to 0 the regex likely broke.
    assert total_sites_checked > 0, (
        "No `.find(` call sites detected across the in-scope routers — the detection "
        "regex may be broken (expected >=1 site per RESEARCH.md's SEC-03 audit)."
    )


def test_no_unbounded_to_list_calls():
    """No `.to_list(None)` and no argless `.to_list()` in any of the five in-scope routers."""
    unbounded_to_list_re = re.compile(r"\.to_list\(\s*None\s*\)|\.to_list\(\s*\)")
    for filename in IN_SCOPE_FILES:
        lines = _read_lines(filename)
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            match = unbounded_to_list_re.search(line)
            assert not match, (
                f"Unbounded `.to_list()`/`.to_list(None)` detected: {filename}:{i + 1} — "
                f"SEC-03 requires `.to_list(length=N)` everywhere."
            )


def test_no_async_for_cursor_streaming():
    """No `async for` cursor-streaming line paired with a `.find(` cursor in the
    five in-scope routers (unbounded iteration bypasses the length cap)."""
    async_for_re = re.compile(r"\basync\s+for\b")
    for filename in IN_SCOPE_FILES:
        lines = _read_lines(filename)
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            match = async_for_re.search(line)
            assert not match, (
                f"`async for` cursor-streaming detected: {filename}:{i + 1} — SEC-03's "
                f"audit found no such pattern; a future `async for cursor` bypasses "
                f"`.to_list(length=N)` bounds and must use bounded `.find(...).to_list(length=N)` "
                f"instead."
            )
