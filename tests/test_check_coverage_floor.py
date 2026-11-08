"""QA-025: the coverage floor script fails below the floor and passes at it."""
import json

from scripts import check_coverage_floor


def _report(tmp_path, percent):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"totals": {"percent_covered": percent}}))
    return str(path)


def test_at_or_above_the_floor_passes(tmp_path):
    assert check_coverage_floor.main(_report(tmp_path, check_coverage_floor.FLOOR_PERCENT)) == 0
    assert check_coverage_floor.main(_report(tmp_path, 99.0)) == 0


def test_below_the_floor_fails(tmp_path):
    assert check_coverage_floor.main(_report(tmp_path, check_coverage_floor.FLOOR_PERCENT - 0.1)) == 1
