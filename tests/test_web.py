from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from funnelcheck.errors import FunnelCheckError
from funnelcheck.pipeline import execute_comparison, execute_run
from funnelcheck.web import create_app, validate_loopback_host

REPO = Path(__file__).parents[1]
EVENTS = REPO / "funnelcheck" / "fixtures" / "events.jsonl"
EVENTS_CANDIDATE = REPO / "funnelcheck" / "fixtures" / "events_candidate.jsonl"
SPEC = REPO / "funnelcheck" / "fixtures" / "spec.yaml"
SPEC_CANDIDATE = REPO / "funnelcheck" / "fixtures" / "spec_candidate.yaml"


@pytest.fixture()
def populated(tmp_path: Path):
    root = tmp_path / "web runs"
    baseline = execute_run(EVENTS, SPEC, root)
    candidate = execute_run(EVENTS_CANDIDATE, SPEC, root)
    variant = execute_run(EVENTS, SPEC_CANDIDATE, root)
    comparison = execute_comparison(baseline.run_id, candidate.run_id, root)
    config_comparison = execute_comparison(baseline.run_id, variant.run_id, root)
    return root, baseline, candidate, comparison, config_comparison


def test_loopback_host_validation() -> None:
    for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        assert validate_loopback_host(host)
    for host in ("0.0.0.0", "127.0.0.2", "192.0.2.1", "example.test", ""):
        with pytest.raises(FunnelCheckError) as exc:
            validate_loopback_host(host)
        assert exc.value.code == "FC_HOST_NOT_ALLOWED"


def test_index_and_api(populated) -> None:
    root, baseline, candidate, comparison, config_comparison = populated
    client = TestClient(create_app(root))
    index = client.get("/")
    assert index.status_code == 200
    assert "运行台账" in index.text
    assert baseline.run_id in index.text
    assert comparison.comparison_id in index.text
    assert "app.css" in index.text
    assert "https://" not in index.text
    assert "default-src 'none'" in index.headers["content-security-policy"]

    api = client.get("/api/runs")
    assert api.status_code == 200
    assert len(api.json()["runs"]) == 3
    detail = client.get(f"/api/runs/{baseline.run_id}")
    assert detail.status_code == 200
    assert detail.json()["run_id"] == baseline.run_id
    assert detail.json()["funnel"]["entered_users"] == 8

    comparisons = client.get("/api/comparisons")
    assert len(comparisons.json()["comparisons"]) == 2
    health = client.get("/healthz")
    assert health.json()["read_only"] is True
    assert health.json()["bind"] == "loopback-only"


def test_run_and_comparison_pages(populated) -> None:
    root, baseline, _candidate, comparison, config_comparison = populated
    client = TestClient(create_app(root))
    page = client.get(f"/runs/{baseline.run_id}")
    assert page.status_code == 200
    assert "订阅转化与回访留存" in page.text
    assert "数据质量" in page.text
    assert "D1" in page.text
    assert "synthetic-search" in page.text
    assert "http://" not in page.text and "https://" not in page.text

    report = client.get(f"/runs/{baseline.run_id}/report.html")
    assert report.status_code == 200
    assert "<style>" in report.text
    markdown = client.get(f"/runs/{baseline.run_id}/report.md")
    assert markdown.status_code == 200
    assert "## 漏斗" in markdown.text

    compare_page = client.get(f"/comparisons/{comparison.comparison_id}")
    assert compare_page.status_code == 200
    assert "input_changed" in compare_page.text
    assert "指标差异" in compare_page.text

    config_page = client.get(f"/comparisons/{config_comparison.comparison_id}")
    assert "config_changed" in config_page.text
    assert config_page.status_code == 200

    compare_html = client.get(f"/comparisons/{comparison.comparison_id}/compare.html")
    assert compare_html.status_code == 200
    assert "<style>" in compare_html.text
    compare_md = client.get(f"/comparisons/{comparison.comparison_id}/compare.md")
    assert compare_md.status_code == 200


def test_missing_runs_and_read_only(populated) -> None:
    root, _baseline, _candidate, _comparison, _config = populated
    client = TestClient(create_app(root))
    missing = client.get("/runs/run-00000000000000000000")
    assert missing.status_code == 404
    assert "FC_RUN_NOT_FOUND" in missing.text
    missing_api = client.get("/api/runs/run-00000000000000000000")
    assert missing_api.status_code == 404
    assert missing_api.json()["code"] == "FC_RUN_NOT_FOUND"
    malformed = client.get("/api/runs/not-a-run")
    assert malformed.status_code == 404
    assert client.post("/api/runs").status_code == 405
    assert client.get("/static/app.css").status_code == 200


def test_empty_ledger_starts_with_instructions(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "empty"))
    response = client.get("/")
    assert response.status_code == 200
    assert "还没有运行记录" in response.text
    assert client.get("/healthz").json()["ledger_present"] is False
