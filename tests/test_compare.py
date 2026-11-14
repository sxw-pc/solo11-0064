from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from funnelcheck.engine.compare import compare_reports
from tests.support import make_event, run_once

TWO_STEP = {
    "funnel": {
        "name": "view_to_buy",
        "window": "7d",
        "steps": [
            {"name": "浏览", "event": "view"},
            {"name": "购买", "event": "buy"},
        ],
    }
}


def _reports(tmp_path: Path):
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-09T00:00:00Z"),
        make_event("v2", "u2", "view", "2026-03-01T01:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP)
    candidate = run_once(
        tmp_path,
        events,
        name="candidate",
        funnel={**TWO_STEP["funnel"], "window": "14d"},
    )
    return baseline.report, candidate.report


def test_identical_reports(tmp_path: Path) -> None:
    baseline, _candidate = _reports(tmp_path)
    result = compare_reports(deepcopy(baseline), deepcopy(baseline))
    assert result["verdict"] == "identical"
    assert result["changed_metric_count"] == 0
    assert result["config_changes"] == []
    assert result["flags"] == {
        "config_changed": False,
        "input_changed": False,
        "metrics_changed": False,
        "engine_changed": False,
    }


def test_config_change_is_isolated(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "config_changed"
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["input_changed"] is False
    assert result["flags"]["metrics_changed"] is True
    assert result["input"]["baseline_sha256"] == result["input"]["candidate_sha256"]
    paths = [item["path"] for item in result["config_changes"]]
    assert "spec.funnel.window" in paths
    assert result["changed_metric_count"] > 0
    step_metric = next(
        item for item in result["metrics"] if item["key"] == "funnel.steps.1.users"
    )
    assert step_metric["baseline"] == 0
    assert step_metric["candidate"] == 1


def test_input_change_is_isolated(tmp_path: Path) -> None:
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-02T00:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP).report
    candidate = run_once(tmp_path, events[:1], name="candidate", **TWO_STEP).report
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "input_changed"
    assert result["flags"]["input_changed"] is True
    assert result["flags"]["config_changed"] is False
    assert result["config_changes"] == []
    assert result["changed_metric_count"] > 0


def test_config_and_input_change(tmp_path: Path) -> None:
    events = [
        make_event("v1", "u1", "view", "2026-03-01T00:00:00Z"),
        make_event("b1", "u1", "buy", "2026-03-07T00:00:00Z"),
    ]
    baseline = run_once(tmp_path, events, name="baseline", **TWO_STEP).report
    candidate = run_once(
        tmp_path,
        events[:1],
        name="candidate",
        funnel={**TWO_STEP["funnel"], "window": "14d"},
    ).report
    result = compare_reports(baseline, candidate)
    assert result["verdict"] == "config_and_input_changed"
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["input_changed"] is True


def test_no_metric_change_when_only_title_changes(tmp_path: Path) -> None:
    events = [make_event("v1", "u1", "view", "2026-03-01T00:00:00Z")]
    baseline = run_once(tmp_path, events, name="baseline", title="A", **TWO_STEP).report
    candidate = run_once(tmp_path, events, name="candidate", title="B", **TWO_STEP).report
    result = compare_reports(baseline, candidate)
    assert result["flags"]["config_changed"] is True
    assert result["flags"]["metrics_changed"] is False
    assert result["verdict"] == "no_metric_change"
    assert result["config_changes"][0]["path"] == "spec.title"


def test_engine_and_unexplained_verdicts(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    engine_changed = deepcopy(candidate)
    engine_changed["engine"]["semantics_version"] = "future-semantics"
    result = compare_reports(baseline, engine_changed)
    assert result["verdict"] == "engine_changed"
    assert result["flags"]["engine_changed"] is True

    unexplained = deepcopy(baseline)
    unexplained["funnel"]["converted_users"] += 1
    result = compare_reports(baseline, unexplained)
    assert result["verdict"] == "unexplained_metric_change"
    assert result["flags"] == {
        "config_changed": False,
        "input_changed": False,
        "metrics_changed": True,
        "engine_changed": False,
    }


def test_comparison_id_is_deterministic_and_summary_present(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    first = compare_reports(baseline, candidate)
    second = compare_reports(baseline, candidate)
    assert first["comparison_id"] == second["comparison_id"]
    assert first["comparison_id"].startswith("cmp-")
    assert first["baseline"]["run_id"] == baseline["run_id"]
    assert first["candidate"]["run_id"] == candidate["run_id"]
    assert first["baseline"]["funnel"]["entered_users"] == 2
    assert "D1" in first["baseline"]["retention"]["periods"] or first["baseline"]["retention"]["periods"] == {}
    assert isinstance(first["quality_changes"], list)
