from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from funnelcheck.cli import main
from funnelcheck.ledger import list_comparisons, list_runs
from funnelcheck.pipeline import ledger_path

REPO = Path(__file__).parents[1]
EVENTS = REPO / "funnelcheck" / "fixtures" / "events.jsonl"
SPEC = REPO / "funnelcheck" / "fixtures" / "spec.yaml"


def test_demo_command_builds_runs_and_comparisons(tmp_path: Path, capsys) -> None:
    root = tmp_path / "demo-runs"
    code = main(["demo", "--runs-dir", str(root)])
    output = capsys.readouterr().out
    assert code == 0
    assert "规格变化" in output and "输入变化" in output
    runs = list_runs(ledger_path(root))
    comparisons = list_comparisons(ledger_path(root))
    assert len(runs) == 3
    assert len(comparisons) == 2
    assert {row["verdict"] for row in comparisons} == {"config_changed", "input_changed"}
    for row in runs:
        directory = root / row["run_id"]
        assert (directory / "report.json").is_file()
        assert (directory / "report.html").is_file()
    assert (root / "ledger.sqlite").is_file()

    code = main(["demo", "--runs-dir", str(root)])
    assert code == 0
    assert len(list_runs(ledger_path(root))) == 3
    assert len(list_comparisons(ledger_path(root))) == 2


def test_run_list_and_compare_commands(tmp_path: Path, capsys) -> None:
    root = tmp_path / "runs"
    assert main(["run", "--events", str(EVENTS), "--spec", str(SPEC), "--runs-dir", str(root)]) == 0
    run_output = capsys.readouterr().out
    assert "运行完成" in run_output
    runs = list_runs(ledger_path(root))
    assert len(runs) == 1
    run_id = runs[0]["run_id"]

    assert main(["list", "--runs-dir", str(root)]) == 0
    list_output = capsys.readouterr().out
    assert run_id in list_output
    assert "运行：" in list_output

    assert main(["compare", "--baseline", run_id, "--candidate", run_id, "--runs-dir", str(root)]) == 0
    compare_output = capsys.readouterr().out
    assert "identical" in compare_output
    assert len(list_comparisons(ledger_path(root))) == 1


def test_errors_and_serve_guard(tmp_path: Path, capsys, monkeypatch) -> None:
    code = main(
        [
            "run",
            "--events",
            str(tmp_path / "missing.jsonl"),
            "--spec",
            str(SPEC),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert code != 0
    assert "FC_IO" in capsys.readouterr().err

    code = main(
        ["serve", "--host", "0.0.0.0", "--port", "8765", "--runs-dir", str(tmp_path / "runs")]
    )
    assert code != 0
    assert "FC_HOST_NOT_ALLOWED" in capsys.readouterr().err

    calls: list[tuple] = []

    def fake_serve(root, host, port, *, log_level):
        calls.append((root, host, port, log_level))

    monkeypatch.setattr("funnelcheck.cli.serve", fake_serve)
    code = main(
        ["serve", "--host", "127.0.0.1", "--port", "8766", "--runs-dir", str(tmp_path / "runs")]
    )
    assert code == 0
    assert calls == [(str(tmp_path / "runs"), "127.0.0.1", 8766, "warning")]


def test_module_entrypoint_reports_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "funnelcheck", "--version"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "1.0.0" in result.stdout
