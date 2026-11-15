"""产物/台账一致性审计与两种修复语义的结构化测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from funnelcheck.audit import audit_runs, repair_runs
from funnelcheck.cli import main
from funnelcheck.errors import FunnelCheckError
from funnelcheck.ledger import get_run, list_runs
from funnelcheck.pipeline import execute_run, ledger_path

REPO = Path(__file__).parents[1]
EVENTS = REPO / "funnelcheck" / "fixtures" / "events.jsonl"
EVENTS_CANDIDATE = REPO / "funnelcheck" / "fixtures" / "events_candidate.jsonl"
SPEC = REPO / "funnelcheck" / "fixtures" / "spec.yaml"
SPEC_CANDIDATE = REPO / "funnelcheck" / "fixtures" / "spec_candidate.yaml"

CREATED = "2026-11-15T00:00:00Z"


def make_run(root: Path, *, events: Path = EVENTS, spec: Path = SPEC, write_ledger: bool = True) -> str:
    outcome = execute_run(events, spec, root, write_ledger=write_ledger, created_at=CREATED)
    return outcome.run_id


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def only_record(result: dict) -> dict:
    assert result["summary"]["total"] == 1
    return result["records"][0]


def ledger_ids(root: Path) -> set[str]:
    return {row["run_id"] for row in list_runs(ledger_path(root))}


def test_audit_healthy_run_is_ok(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_id = make_run(root)
    result = audit_runs(root)
    assert result["summary"]["clean"] is True
    record = only_record(result)
    assert record["run_id"] == run_id
    assert record["status"] == "ok"
    assert record["issues"] == []
    assert record["recoverable"] is False
    assert record["has_directory"] and record["has_ledger_row"]
    assert record["ledger_summary_match"] is True
    assert record["identities"] == {
        "directory": run_id,
        "manifest": run_id,
        "report": run_id,
        "ledger": run_id,
    }
    for name in ("report.json", "report.md", "report.html"):
        check = record["artifacts"][name]
        assert check["present"] and check["listed"]
        assert check["sha256_ok"] and check["bytes_ok"]


def test_audit_orphan_from_crash_between_manifest_and_ledger_commit(tmp_path: Path) -> None:
    """断点一：manifest 落盘后、SQLite 提交前退出 → 产物完整但台账为空。"""
    root = tmp_path / "runs"
    run_id = make_run(root, write_ledger=False)
    before = snapshot_tree(root)

    result = audit_runs(root)
    record = only_record(result)
    assert record["status"] == "orphan"
    assert "ledger_missing" in record["issues"]
    assert record["recoverable"] is True
    assert record["has_directory"] and not record["has_ledger_row"]
    # 只读审计：不创建台账、不改任何字节
    assert not ledger_path(root).exists()
    assert snapshot_tree(root) == before
    assert run_id not in ledger_ids(root)


def test_audit_ledger_row_with_deleted_or_rewritten_files(tmp_path: Path) -> None:
    """断点二：台账已提交，但产物目录被删或被改写。"""
    root = tmp_path / "runs"
    run_id = make_run(root)
    shutil.rmtree(root / run_id)
    record = only_record(audit_runs(root))
    assert record["status"] == "ledger_only"
    assert "directory_missing" in record["issues"]
    assert record["recoverable"] is False

    run_id_two = make_run(root, events=EVENTS_CANDIDATE)
    (root / run_id_two / "report.md").write_text("被改写\n", encoding="utf-8")
    records = {item["run_id"]: item for item in audit_runs(root)["records"]}
    rewritten = records[run_id_two]
    # 台账 ID 存在不能掩盖产物损坏
    assert rewritten["status"] == "corrupt"
    assert "hash_mismatch:report.md" in rewritten["issues"]
    assert rewritten["recoverable"] is False
    assert records[run_id]["status"] == "ledger_only"


def test_audit_hash_and_summary_corruption(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_id = make_run(root)
    report_path = root / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["funnel"]["entered_users"] = 999
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    record = only_record(audit_runs(root))
    assert record["status"] == "corrupt"
    assert "hash_mismatch:report.json" in record["issues"]
    assert "bytes_mismatch:report.json" in record["issues"]
    # 台账摘要与被改写的 report.json 重算结果不一致，差异必须可见
    assert "ledger_summary_mismatch" in record["issues"]
    assert record["ledger_summary_match"] is False
    assert record["artifacts"]["report.json"]["sha256_ok"] is False
    assert record["artifacts"]["report.md"]["sha256_ok"] is True


def test_audit_run_id_mismatch_is_never_merged_into_healthy(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_id = make_run(root)
    fake_id = "run-" + "0" * 20

    manifest_path = root / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = fake_id
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = audit_runs(root)
    record = only_record(result)
    assert record["status"] == "id_mismatch"
    assert "run_id_mismatch:manifest" in record["issues"]
    assert record["identities"]["manifest"] == fake_id
    assert record["identities"]["directory"] == run_id
    # 伪造身份不会生成第二条记录，更不会是健康记录
    assert fake_id not in {item["run_id"] for item in result["records"]}
    assert result["summary"]["clean"] is False

    report_path = root / run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["run_id"] = fake_id
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同步更新 manifest 摘要，隔离出纯粹的 report 身份不一致
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = run_id
    payload = report_path.read_bytes()
    manifest["artifacts"]["report.json"] = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    record = only_record(audit_runs(root))
    assert record["status"] == "id_mismatch"
    assert "run_id_mismatch:report" in record["issues"]


def test_audit_ledger_artifact_dir_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_id = make_run(root)
    with sqlite3.connect(ledger_path(root)) as connection:
        connection.execute(
            "UPDATE runs SET artifact_dir = ? WHERE run_id = ?", ("elsewhere", run_id)
        )
    record = only_record(audit_runs(root))
    assert record["status"] == "id_mismatch"
    assert "ledger_artifact_dir_mismatch" in record["issues"]


def test_audit_duplicate_runs_stay_single_healthy_record(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    first = make_run(root)
    second = make_run(root)  # 同输入重复运行：台账 upsert，不重复
    assert first == second
    result = audit_runs(root)
    assert result["summary"]["clean"] is True
    assert only_record(result)["status"] == "ok"
    assert len(list_runs(ledger_path(root))) == 1


def test_audit_stale_ledger_summary_mismatch(tmp_path: Path) -> None:
    """旧台账：行存在但指纹/摘要与当前产物不一致。"""
    root = tmp_path / "runs"
    run_id = make_run(root)
    with sqlite3.connect(ledger_path(root)) as connection:
        connection.execute(
            "UPDATE runs SET input_sha256 = ? WHERE run_id = ?", ("stale", run_id)
        )
    record = only_record(audit_runs(root))
    assert record["status"] == "summary_mismatch"
    assert "ledger_mismatch:input_sha256" in record["issues"]
    assert record["recoverable"] is False


def test_repair_manifest_mode_backfills_verified_orphan(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run_id = make_run(root, write_ledger=False)

    dry = repair_runs(root, mode="manifest", dry_run=True)
    assert [action["run_id"] for action in dry["actions"]] == [run_id]
    assert dry["actions"][0]["applied"] is False
    assert list_runs(ledger_path(root)) == []  # dry-run 不落库

    applied = repair_runs(root, mode="manifest", dry_run=False, created_at=CREATED)
    assert applied["actions"][0]["applied"] is True
    row = get_run(ledger_path(root), run_id)
    assert row is not None
    assert row["artifact_dir"] == run_id
    assert row["created_at_utc"] == CREATED
    # 补登记后进入 history，复审计干净
    assert run_id in ledger_ids(root)
    assert audit_runs(root)["summary"]["clean"] is True

    again = repair_runs(root, mode="manifest", dry_run=False, created_at="2026-12-01T00:00:00Z")
    assert again["actions"] == []  # run_id 幂等：第二次无可执行变更
    assert get_run(ledger_path(root), run_id)["created_at_utc"] == CREATED


def test_repair_ledger_mode_reports_orphan_without_backfill(tmp_path: Path) -> None:
    """同一孤儿：ledger 权威不进入 history，manifest 权威进入 —— 差异可解释。"""
    root = tmp_path / "runs"
    run_id = make_run(root, write_ledger=False)

    plan = repair_runs(root, mode="ledger", dry_run=False)
    assert plan["actions"] == []
    assert [item["run_id"] for item in plan["skipped"]] == [run_id]
    assert "仅报告" in plan["skipped"][0]["reason"]
    assert list_runs(ledger_path(root)) == []  # 不进入 history
    assert only_record(audit_runs(root))["status"] == "orphan"

    repair_runs(root, mode="manifest", dry_run=False, created_at=CREATED)
    assert run_id in ledger_ids(root)


def test_repair_never_overwrites_existing_ledger_row(tmp_path: Path) -> None:
    """台账已存在但产物损坏：两种模式都不覆盖已有结果、不伪造校验通过。"""
    root = tmp_path / "runs"
    run_id = make_run(root)
    before_row = get_run(ledger_path(root), run_id)
    (root / run_id / "report.md").write_text("损坏\n", encoding="utf-8")
    assert only_record(audit_runs(root))["status"] == "corrupt"

    for mode in ("manifest", "ledger"):
        plan = repair_runs(root, mode=mode, dry_run=False, created_at="2026-12-01T00:00:00Z")
        assert plan["actions"] == []
        assert [item["run_id"] for item in plan["skipped"]] == [run_id]
        assert get_run(ledger_path(root), run_id) == before_row
        assert only_record(audit_runs(root))["status"] == "corrupt"


def test_audit_and_dry_run_repair_are_read_only(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    make_run(root)  # 健康
    orphan_id = make_run(root, events=EVENTS_CANDIDATE, write_ledger=False)  # 孤儿
    corrupt_id = make_run(root, spec=SPEC_CANDIDATE)  # 损坏（台账在、产物被改）
    (root / corrupt_id / "report.html").write_text("损坏\n", encoding="utf-8")
    before = snapshot_tree(root)

    result = audit_runs(root)
    assert result["summary"]["by_status"] == {"ok": 1, "orphan": 1, "corrupt": 1}
    for mode in ("manifest", "ledger"):
        repair_runs(root, mode=mode, dry_run=True)
    assert snapshot_tree(root) == before
    assert orphan_id not in ledger_ids(root)


def test_audit_unreadable_ledger_is_reported_not_silent(tmp_path: Path) -> None:
    """台账文件损坏：显式上报 ledger_error，而不是静默当作空台账。"""
    root = tmp_path / "runs"
    make_run(root)
    ledger_path(root).write_bytes(b"not a sqlite database")
    result = audit_runs(root)
    assert result["ledger_error"] is not None
    assert result["summary"]["clean"] is False
    # 台账证据缺失时健康运行也不能合并成 ok
    assert only_record(result)["status"] == "orphan"


def test_audit_malformed_summary_json_does_not_crash(tmp_path: Path) -> None:
    """台账行 summary_json 损坏：审计不崩溃，差异可见。"""
    root = tmp_path / "runs"
    run_id = make_run(root)
    with sqlite3.connect(ledger_path(root)) as connection:
        connection.execute(
            "UPDATE runs SET summary_json = ? WHERE run_id = ?", ("{not-json", run_id)
        )
    record = only_record(audit_runs(root))
    assert record["status"] == "summary_mismatch"
    assert "ledger_summary_mismatch" in record["issues"]


def test_audit_invalid_repair_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(FunnelCheckError):
        repair_runs(tmp_path, mode="sideways")


def test_cli_audit_runs_exit_codes_and_json(tmp_path: Path, capsys) -> None:
    root = tmp_path / "runs"
    run_id = make_run(root)
    assert main(["audit-runs", "--runs-dir", str(root)]) == 0
    assert run_id in capsys.readouterr().out

    orphan_id = make_run(root, events=EVENTS_CANDIDATE, write_ledger=False)
    assert main(["audit-runs", "--runs-dir", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["by_status"] == {"ok": 1, "orphan": 1}
    statuses = {item["run_id"]: item["status"] for item in payload["records"]}
    assert statuses == {run_id: "ok", orphan_id: "orphan"}

    # dry-run 默认：列出变更但不落库，退出码仍反映未修复状态
    assert main(["audit-runs", "--runs-dir", str(root), "--repair", "manifest"]) == 1
    output = capsys.readouterr().out
    assert "dry-run" in output and "backfill_ledger" in output
    assert orphan_id not in ledger_ids(root)

    assert main(["audit-runs", "--runs-dir", str(root), "--repair", "manifest", "--apply"]) == 0
    assert orphan_id in ledger_ids(root)
    assert main(["audit-runs", "--runs-dir", str(root)]) == 0


def test_cli_audit_apply_requires_repair(tmp_path: Path, capsys) -> None:
    code = main(["audit-runs", "--runs-dir", str(tmp_path / "runs"), "--apply"])
    assert code == 2
    assert "FC_USAGE" in capsys.readouterr().err
