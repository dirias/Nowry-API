"""
Check pip-audit Severity Gate

pip-audit itself has NO severity concept: it exits 1 on ANY vulnerability
regardless of severity, and its JSON report never includes a severity field.
This script cross-references each finding's GHSA alias against the public
GitHub Advisories API (which does publish severity) and turns pip-audit's
severity-blind report into a High/Critical blocking decision, leaving
Low/Medium/unresolved findings advisory.

Usage (run from Nowry-API/, after `pip-audit -r requirements.lock -f json -o pip-audit-report.json`):
    python scripts/check_pip_audit_severity.py pip-audit-report.json
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any

FAIL_SEVERITIES = {"high", "critical"}
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")


def ghsa_severity(ghsa_id: str) -> str | None:
    """Fetch the severity for a GHSA advisory ID from the GitHub Advisories API.

    Returns the lowercase severity string (e.g. "high"), or None if the
    severity could not be resolved (network error, missing field, non-2xx
    response, etc.). Any failure is fail-open: it prints a WARNING to stderr
    and returns None so the caller treats the finding as advisory rather than
    blocking the build on a transient API failure (T-32-08).
    """
    url = f"https://api.github.com/advisories/{ghsa_id}"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data: dict[str, Any] = json.load(response)
            severity = data.get("severity")
            return severity.lower() if isinstance(severity, str) else None
    except Exception as exc:  # noqa: BLE001 - fail-open on any error (T-32-08)
        print(f"WARNING: could not resolve severity for {ghsa_id}: {exc}", file=sys.stderr)
        return None


def main(report_path: str) -> int:
    """Classify a pip-audit JSON report's findings as blocking or advisory.

    Returns 1 if any finding resolves to a High/Critical GHSA severity
    (blocking), else 0 (advisory — including unresolved/Low/Medium findings).
    """
    with open(report_path, "r", encoding="utf-8") as fh:
        report: dict[str, Any] = json.load(fh)

    blocking: list[str] = []
    advisory: list[str] = []

    for dependency in report.get("dependencies", []):
        name = dependency.get("name", "<unknown>")
        version = dependency.get("version", "<unknown>")
        for vuln in dependency.get("vulns", []):
            ghsa_id = next(
                (alias for alias in vuln.get("aliases", []) if alias.startswith("GHSA-")),
                None,
            )
            vuln_id = vuln.get("id", "<unknown>")
            label = f"{name}=={version} ({vuln_id})"

            if ghsa_id is None:
                advisory.append(f"{label} — no GHSA alias found, cannot resolve severity")
                continue

            severity = ghsa_severity(ghsa_id)
            if severity is None:
                advisory.append(f"{label} — {ghsa_id} severity unresolved (fail-open)")
            elif severity in FAIL_SEVERITIES:
                blocking.append(f"{label} — {ghsa_id} severity={severity}")
            else:
                advisory.append(f"{label} — {ghsa_id} severity={severity}")

    print(f"Blocking (High/Critical) findings: {len(blocking)}")
    for entry in blocking:
        print(f"  BLOCKING: {entry}")

    print(f"Advisory (Low/Medium/unresolved) findings: {len(advisory)}")
    for entry in advisory:
        print(f"  advisory: {entry}")

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pip-audit-report.json"))
