"""Tests for the pip-audit severity gate — SEC-02.

pip-audit exits 1 on ANY vulnerability regardless of severity and never
publishes a severity field in its JSON report. check_pip_audit_severity.py
cross-references each finding's GHSA alias against the GitHub Advisories API
to decide High/Critical (blocking) vs Low/Medium/unresolved (advisory,
fail-open per T-32-08).

Each test imports scripts.check_pip_audit_severity inside the test body
(matching the test_sync_langfuse.py pattern) and patches the network call at
scripts.check_pip_audit_severity.urllib.request.urlopen — never hitting the
live GitHub API.
"""
import json
from unittest.mock import MagicMock, patch


def _fake_report(dependencies):
    """Build a pip-audit-report.json-shaped dict from a list of
    (name, version, vulns) tuples where vulns is a list of
    (vuln_id, aliases) tuples."""
    return {
        "dependencies": [
            {
                "name": name,
                "version": version,
                "vulns": [
                    {"id": vuln_id, "aliases": aliases}
                    for vuln_id, aliases in vulns
                ],
            }
            for name, version, vulns in dependencies
        ]
    }


def _mock_urlopen_returning(payload):
    """Build a MagicMock urlopen() replacement whose context-manager __enter__
    yields an object such that json.load(resp) returns `payload`."""
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = response
    return MagicMock(return_value=cm)


def test_high_severity_finding_is_blocking(tmp_path):
    """A High-severity GHSA finding causes main() to return 1 (blocking)."""
    from scripts import check_pip_audit_severity

    report = _fake_report(
        [("requests", "2.25.0", [("PYSEC-2021-1", ["GHSA-abcd-1234-5678"])])]
    )
    report_path = tmp_path / "pip-audit-report.json"
    report_path.write_text(json.dumps(report))

    with patch(
        "scripts.check_pip_audit_severity.urllib.request.urlopen",
        _mock_urlopen_returning({"severity": "high"}),
    ):
        result = check_pip_audit_severity.main(str(report_path))

    assert result == 1


def test_low_or_medium_severity_finding_is_advisory(tmp_path):
    """A Low/Medium-severity GHSA finding causes main() to return 0 (advisory)."""
    from scripts import check_pip_audit_severity

    report = _fake_report(
        [("jinja2", "3.0.0", [("PYSEC-2022-2", ["GHSA-efgh-2345-6789"])])]
    )
    report_path = tmp_path / "pip-audit-report.json"
    report_path.write_text(json.dumps(report))

    with patch(
        "scripts.check_pip_audit_severity.urllib.request.urlopen",
        _mock_urlopen_returning({"severity": "medium"}),
    ):
        result = check_pip_audit_severity.main(str(report_path))

    assert result == 0


def test_unresolvable_severity_fails_open(tmp_path):
    """A finding whose severity cannot be resolved (urlopen raises) is
    treated as advisory, not blocking — fail-open per T-32-08."""
    from scripts import check_pip_audit_severity

    report = _fake_report(
        [("pyyaml", "5.3.0", [("PYSEC-2020-3", ["GHSA-ijkl-3456-7890"])])]
    )
    report_path = tmp_path / "pip-audit-report.json"
    report_path.write_text(json.dumps(report))

    with patch(
        "scripts.check_pip_audit_severity.urllib.request.urlopen",
        side_effect=TimeoutError("network unreachable"),
    ):
        result = check_pip_audit_severity.main(str(report_path))

    assert result == 0


def test_missing_ghsa_alias_fails_open(tmp_path):
    """A vuln with no GHSA-prefixed alias cannot be resolved and is advisory."""
    from scripts import check_pip_audit_severity

    report = _fake_report(
        [("numpy", "1.20.0", [("PYSEC-2021-9", ["CVE-2021-99999"])])]
    )
    report_path = tmp_path / "pip-audit-report.json"
    report_path.write_text(json.dumps(report))

    with patch(
        "scripts.check_pip_audit_severity.urllib.request.urlopen",
    ) as mock_urlopen:
        result = check_pip_audit_severity.main(str(report_path))

    assert result == 0
    mock_urlopen.assert_not_called()


def test_empty_report_returns_zero(tmp_path):
    """An empty pip-audit report (no dependencies/vulns) returns 0."""
    from scripts import check_pip_audit_severity

    report = _fake_report([])
    report_path = tmp_path / "pip-audit-report.json"
    report_path.write_text(json.dumps(report))

    with patch(
        "scripts.check_pip_audit_severity.urllib.request.urlopen",
    ) as mock_urlopen:
        result = check_pip_audit_severity.main(str(report_path))

    assert result == 0
    mock_urlopen.assert_not_called()
