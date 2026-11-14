from __future__ import annotations

import hashlib
import json
from pathlib import Path

from funnelcheck.ledger import (
    get_comparison,
    get_run,
    list_comparisons,
    list_runs,
    record_run,
    run_summary,
)
from funnelcheck.pipeline import (
    execute_comparison,
    execute_run,
    ledger_path,
    load_comparison,
    load_run_report,
)
from funnelcheck.report import dump_json

REPO = Path(__file__).parents[1]
EVENTS = REPO / "funnelcheck" / "fixtures" / "events.jsonl"
EVENTS_CANDIDATE = REPO / "funnelcheck" / "fixtures" / "events_candidate.jsonl"
SPEC = REPO / "funnelcheck" / "fixtures" / "spec.yaml"


def test_run_artifacts_are_deterministic_and_complete(tmp_path: Path) -> None:
    root_one = tmp_path / "runs one"
    root_two = tmp_path / "runs-two"
    first = execute_run(EVENTS, SPEC, root_one, created_at="2026-11-15T00:00:00Z")
    second = execute_run(EVENTS, SPEC, root_two, created_at="2026-11-15T00:00:01Z")
    assert first.run_id == second.run_id
    assert set(first.written) == {"report.json", "report.md", "report.html", "manifest.json"}
    for name in ("report.json", "report.md", "report.html", "manifest.json"):
        assert (first.artifact_dir / name).read_bytes() == (second.artifact_dir / name).read_bytes()

    report_bytes = (first.artifact_dir / "report.json").read_text(encoding="utf-8")
    assert report_bytes == dump_json(first.report)
    report = json.loads(report_bytes)
    assert report["funnel"]["entered_users"] == 8
    assert report["retention"]["cohort_size"] == 8
    assert str(tmp_path) not in report_bytes

    markdown = (first.artifact_dir / "report.md").read_text(encoding="utf-8")
    assert "## 漏斗" in markdown and "## 留存" in markdown and "## 数据质量" in markdown
    html = (first.artifact_dir / "report.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "<script" not in html
    assert "http://" not in html and "https://" not in html

    manifest = json.loads((first.artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["run_id"] == first.run_id
    for name, metadata in manifest["artifacts"].items():
        payload = (first.artifact_dir / name).read_bytes()
        assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
        assert metadata["bytes"] == len(payload)

    row = get_run(ledger_path(root_one), first.run_id)
    assert row is not None
    assert row["created_at_utc"] == "2026-11-15T00:00:00Z"
    assert row["artifact_dir"] == first.run_id
    assert row["summary"]["funnel"]["entered_users"] == 8
    assert row["summary"]["quality"]["included_events"] == 38


def test_ledger_upsert_and_listing(tmp_path: Path) -> None:
    root = tmp_path / "ledger"
    outcome = execute_run(EVENTS, SPEC, root, created_at="2026-11-15T00:00:00Z")
    record_run(
        ledger_path(root),
        outcome.report,
        artifact_dir=outcome.run_id,
        created_at="2026-11-15T01:00:00Z",
    )
    rows = list_runs(ledger_path(root))
    assert len(rows) == 1
    assert rows[0]["created_at_utc"] == "2026-11-15T01:00:00Z"
    assert rows[0]["summary"] == run_summary(outcome.report)
    assert load_run_report(root, outcome.run_id)["run_id"] == outcome.run_id


def test_comparison_artifacts_and_ledger(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    baseline = execute_run(EVENTS, SPEC, root)
    candidate = execute_run(EVENTS_CANDIDATE, SPEC, root)
    outcome = execute_comparison(
        baseline.run_id,
        candidate.run_id,
        root,
        created_at="2026-11-15T02:00:00Z",
    )
    assert outcome.comparison["verdict"] == "input_changed"
    assert (outcome.artifact_dir / "compare.json").is_file()
    assert (outcome.artifact_dir / "compare.md").is_file()
    assert (outcome.artifact_dir / "compare.html").is_file()
    assert (outcome.artifact_dir / "manifest.json").is_file()
    html = (outcome.artifact_dir / "compare.html").read_text(encoding="utf-8")
    assert "input_changed" in html
    assert "http://" not in html and "https://" not in html
    loaded = load_comparison(root, outcome.comparison_id)
    assert loaded["comparison_id"] == outcome.comparison_id
    row = get_comparison(ledger_path(root), outcome.comparison_id)
    assert row is not None
    assert row["created_at_utc"] == "2026-11-15T02:00:00Z"
    assert row["verdict"] == "input_changed"
    assert row["artifact_dir"] == f"comparisons/{outcome.comparison_id}"
    assert len(list_comparisons(ledger_path(root))) == 1
