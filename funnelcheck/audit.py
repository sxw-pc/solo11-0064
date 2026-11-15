"""运行产物与 SQLite 台账的一致性审计，以及两种权威语义下的显式修复。

权威边界：report.py 先把 report.json/report.md/report.html 与带 SHA256 摘要的
manifest.json 原子落盘，pipeline.py 随后才提交 SQLite 台账。进程若在两个提交点
之间退出，磁盘上会留下“产物完整但台账缺失”的孤儿运行；反过来，台账行也可能指向
已被删除或改写的产物。本模块只读地交叉核对目录、manifest、三份报告与台账行，
按 run_id 输出可复核状态；目录名、报告 run_id、manifest run_id、台账 run_id
任意不一致都不会被合并成一条健康记录。

修复提供两种显式语义：

- ``manifest``：manifest 作为完整运行的恢复依据，校验通过的孤儿可补登记进台账；
- ``ledger``：台账作为已提交运行的权威，孤儿只报告、不补登记。

两种模式都不覆盖已存在的台账行、不伪造校验通过；修复默认 dry-run，精确列出
变更，按 run_id 幂等。只读审计绝不修改文件或数据库。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import sha256_bytes
from .errors import ErrorCode, FunnelCheckError
from .ledger import get_run, record_run, run_summary
from .pipeline import RUN_ID_RE, ledger_path

EXPECTED_ARTIFACTS: tuple[str, ...] = ("report.json", "report.md", "report.html")

STATUS_OK = "ok"
STATUS_ORPHAN = "orphan"
STATUS_LEDGER_ONLY = "ledger_only"
STATUS_INCOMPLETE = "incomplete"
STATUS_CORRUPT = "corrupt"
STATUS_ID_MISMATCH = "id_mismatch"
STATUS_SUMMARY_MISMATCH = "summary_mismatch"

STATUS_LABELS: dict[str, str] = {
    STATUS_OK: "产物与台账一致",
    STATUS_ORPHAN: "产物完整且校验通过，但台账缺失",
    STATUS_LEDGER_ONLY: "台账有记录，但产物目录缺失",
    STATUS_INCOMPLETE: "产物不完整",
    STATUS_CORRUPT: "产物损坏或校验失败",
    STATUS_ID_MISMATCH: "run_id 身份不一致",
    STATUS_SUMMARY_MISMATCH: "台账摘要与产物不一致",
}

REPAIR_MODES: tuple[str, ...] = ("manifest", "ledger")

_CORRUPT_ISSUES = {
    "manifest_invalid",
    "manifest_incomplete",
    "manifest_artifacts_invalid",
    "report_invalid",
}
_CORRUPT_PREFIXES = ("hash_mismatch:", "bytes_mismatch:", "artifact_unreadable:")
_ID_ISSUES = {"dir_name_invalid", "ledger_artifact_dir_mismatch"}
_ID_PREFIXES = ("run_id_mismatch:",)
_INCOMPLETE_ISSUES = {"manifest_missing"}
_INCOMPLETE_PREFIXES = ("artifact_missing:", "manifest_entry_missing:")
_SUMMARY_ISSUES = {"ledger_summary_mismatch"}
_SUMMARY_PREFIXES = ("ledger_mismatch:",)


@dataclass
class RunAudit:
    """单个 run_id 下汇聚的全部证据与结论。"""

    run_id: str
    has_directory: bool = False
    has_ledger_row: bool = False
    identities: dict[str, str | None] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    diffs: list[str] = field(default_factory=list)
    ledger_summary_match: bool | None = None
    status: str = STATUS_OK
    recoverable: bool = False
    _manifest: dict[str, Any] | None = field(default=None, repr=False)
    _report: dict[str, Any] | None = field(default=None, repr=False)
    _ledger_row: dict[str, Any] | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "recoverable": self.recoverable,
            "has_directory": self.has_directory,
            "has_ledger_row": self.has_ledger_row,
            "identities": dict(self.identities),
            "issues": list(self.issues),
            "diffs": list(self.diffs),
            "artifacts": {name: dict(meta) for name, meta in self.artifacts.items()},
            "ledger_summary_match": self.ledger_summary_match,
        }


def _read_json_lenient(
    path: Path, record: RunAudit, *, missing: str, invalid: str
) -> dict[str, Any] | None:
    if not path.is_file():
        record.issues.append(missing)
        record.diffs.append(f"缺少文件：{path.name}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        record.issues.append(invalid)
        record.diffs.append(f"{path.name} 不是有效 JSON：{exc}")
        return None
    if not isinstance(payload, dict):
        record.issues.append(invalid)
        record.diffs.append(f"{path.name} 根节点不是对象")
        return None
    return payload


def _check_manifest(
    directory: Path, manifest: dict[str, Any], record: RunAudit
) -> None:
    manifest_run_id = manifest.get("run_id")
    record.identities["manifest"] = (
        manifest_run_id if isinstance(manifest_run_id, str) else None
    )
    if manifest_run_id != record.run_id:
        record.issues.append("run_id_mismatch:manifest")
        record.diffs.append(
            f"manifest.json 的 run_id={manifest_run_id!r} 与目录名 {record.run_id!r} 不一致"
        )
    if manifest.get("status") != "complete":
        record.issues.append("manifest_incomplete")
        record.diffs.append(f"manifest status={manifest.get('status')!r}，不是 complete")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        record.issues.append("manifest_artifacts_invalid")
        record.diffs.append("manifest.json 缺少 artifacts 摘要表")
        return
    for name in EXPECTED_ARTIFACTS:
        meta = artifacts.get(name)
        path = directory / name
        check: dict[str, Any] = {"listed": isinstance(meta, dict), "present": path.is_file()}
        if not isinstance(meta, dict):
            record.issues.append(f"manifest_entry_missing:{name}")
            record.diffs.append(f"manifest.json 缺少 {name} 的摘要条目")
            record.artifacts[name] = check
            continue
        check["expected_sha256"] = meta.get("sha256")
        check["expected_bytes"] = meta.get("bytes")
        if not path.is_file():
            record.artifacts[name] = check
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            record.issues.append(f"artifact_unreadable:{name}")
            record.diffs.append(f"{name} 无法读取：{exc}")
            record.artifacts[name] = check
            continue
        actual_sha256 = sha256_bytes(data)
        check["sha256"] = actual_sha256
        check["bytes"] = len(data)
        check["sha256_ok"] = actual_sha256 == meta.get("sha256")
        check["bytes_ok"] = len(data) == meta.get("bytes")
        if not check["sha256_ok"]:
            record.issues.append(f"hash_mismatch:{name}")
            record.diffs.append(
                f"{name} 的 SHA256 与 manifest 不符："
                f"实际 {actual_sha256[:12]}… ≠ 记录 {str(meta.get('sha256'))[:12]}…"
            )
        if not check["bytes_ok"]:
            record.issues.append(f"bytes_mismatch:{name}")
            record.diffs.append(
                f"{name} 的字节数与 manifest 不符：实际 {len(data)} ≠ 记录 {meta.get('bytes')}"
            )
        record.artifacts[name] = check


def _inspect_directory(directory: Path, record: RunAudit) -> None:
    if not RUN_ID_RE.match(record.run_id):
        record.issues.append("dir_name_invalid")
        record.diffs.append(f"目录名不符合 run-<20位十六进制> 规范：{record.run_id}")

    manifest = _read_json_lenient(
        directory / "manifest.json",
        record,
        missing="manifest_missing",
        invalid="manifest_invalid",
    )
    report = _read_json_lenient(
        directory / "report.json",
        record,
        missing="artifact_missing:report.json",
        invalid="report_invalid",
    )
    record._manifest = manifest
    record._report = report

    for name in ("report.md", "report.html"):
        if not (directory / name).is_file():
            record.issues.append(f"artifact_missing:{name}")
            record.diffs.append(f"缺少产物文件：{name}")

    if manifest is not None:
        _check_manifest(directory, manifest, record)
    if report is not None:
        report_run_id = report.get("run_id")
        record.identities["report"] = (
            report_run_id if isinstance(report_run_id, str) else None
        )
        if report_run_id != record.run_id:
            record.issues.append("run_id_mismatch:report")
            record.diffs.append(
                f"report.json 的 run_id={report_run_id!r} 与目录名 {record.run_id!r} 不一致"
            )


def _cross_check_ledger(record: RunAudit) -> None:
    row = record._ledger_row
    if row is None:
        if record.has_directory:
            record.issues.append("ledger_missing")
            record.diffs.append(
                "台账中没有该 run_id 的记录（疑似 manifest 落盘后、台账提交前退出）"
            )
        return
    if not record.has_directory:
        record.issues.append("directory_missing")
        record.diffs.append("台账存在记录，但产物目录缺失")
        return
    manifest = record._manifest
    if manifest is not None:
        for key in ("input_sha256", "normalized_sha256", "spec_sha256"):
            ledger_value = row.get(key)
            manifest_value = manifest.get(key)
            if ledger_value != manifest_value:
                record.issues.append(f"ledger_mismatch:{key}")
                record.diffs.append(
                    f"{key} 不一致：台账={ledger_value!r} 产物={manifest_value!r}"
                )
    report = record._report
    if report is not None and report.get("run_id") == record.run_id:
        try:
            expected = run_summary(report)
        except (AttributeError, TypeError):
            record.ledger_summary_match = False
            record.issues.append("ledger_summary_mismatch")
            record.diffs.append("report.json 结构异常，无法重算摘要与台账比对")
            return
        record.ledger_summary_match = row.get("summary") == expected
        if not record.ledger_summary_match:
            record.issues.append("ledger_summary_mismatch")
            record.diffs.append("台账 summary 与 report.json 重算摘要不一致")


def _finalize(record: RunAudit) -> None:
    issues = set(record.issues)

    def any_prefix(prefixes: tuple[str, ...]) -> bool:
        return any(issue.startswith(prefixes) for issue in issues)

    if issues & _CORRUPT_ISSUES or any_prefix(_CORRUPT_PREFIXES):
        record.status = STATUS_CORRUPT
    elif issues & _ID_ISSUES or any_prefix(_ID_PREFIXES):
        record.status = STATUS_ID_MISMATCH
    elif issues & _INCOMPLETE_ISSUES or any_prefix(_INCOMPLETE_PREFIXES):
        record.status = STATUS_INCOMPLETE
    elif issues & _SUMMARY_ISSUES or any_prefix(_SUMMARY_PREFIXES):
        record.status = STATUS_SUMMARY_MISMATCH
    elif "ledger_missing" in issues:
        record.status = STATUS_ORPHAN
    elif "directory_missing" in issues:
        record.status = STATUS_LEDGER_ONLY
    else:
        record.status = STATUS_OK
    # 只有“产物完整通过校验、唯独台账缺失”的孤儿才允许按 manifest 权威补登记。
    record.recoverable = record.status == STATUS_ORPHAN


def _ledger_rows(ledger_file: Path) -> tuple[list[dict[str, Any]], str | None]:
    """只读拉取台账行；summary_json 损坏的行保留 None 摘要而不是让审计崩溃。"""
    if not ledger_file.is_file():
        return [], None
    try:
        uri = f"{ledger_file.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                "SELECT * FROM runs ORDER BY created_at_utc DESC, run_id DESC"
            )
            rows = [dict(row) for row in cursor.fetchall()]
    except (OSError, sqlite3.Error) as exc:
        return [], str(exc)
    for row in rows:
        try:
            row["summary"] = json.loads(row.pop("summary_json"))
        except (KeyError, TypeError, json.JSONDecodeError):
            row["summary"] = None
    return rows, None


def collect_records(root: str | Path) -> tuple[list[RunAudit], str | None]:
    """扫描运行目录与台账，返回按 run_id 排序的审计记录与台账错误（只读）。"""
    root_path = Path(root)
    records: dict[str, RunAudit] = {}

    if root_path.is_dir():
        for entry in sorted(root_path.iterdir()):
            if not entry.is_dir() or entry.name == "comparisons":
                continue
            record = records.setdefault(entry.name, RunAudit(run_id=entry.name))
            record.has_directory = True
            record.identities["directory"] = entry.name
            _inspect_directory(entry, record)

    rows, ledger_error = _ledger_rows(ledger_path(root_path))
    for row in rows:
        run_id = str(row.get("run_id"))
        record = records.setdefault(run_id, RunAudit(run_id=run_id))
        record.has_ledger_row = True
        record.identities["ledger"] = run_id
        record._ledger_row = row
        artifact_dir = row.get("artifact_dir")
        if artifact_dir != run_id:
            record.issues.append("ledger_artifact_dir_mismatch")
            record.diffs.append(
                f"台账 artifact_dir={artifact_dir!r} 与 run_id={run_id!r} 不一致"
            )

    for record in records.values():
        _cross_check_ledger(record)
        _finalize(record)
    return [records[key] for key in sorted(records)], ledger_error


def audit_runs(root: str | Path) -> dict[str, Any]:
    """只读审计：不修改任何文件或数据库，返回结构化、可复核的结果。"""
    root_path = Path(root)
    ledger_file = ledger_path(root_path)
    found, ledger_error = collect_records(root_path)
    records = [record.as_dict() for record in found]
    by_status: dict[str, int] = {}
    for record in records:
        by_status[record["status"]] = by_status.get(record["status"], 0) + 1
    clean = ledger_error is None and all(
        record["status"] == STATUS_OK for record in records
    )
    return {
        "runs_dir": str(root_path),
        "ledger": str(ledger_file),
        "ledger_present": ledger_file.is_file(),
        "ledger_error": ledger_error,
        "records": records,
        "summary": {
            "total": len(records),
            "clean": clean,
            "by_status": by_status,
        },
    }


def _verified_report_for_backfill(directory: Path, run_id: str) -> dict[str, Any] | None:
    """补登记前的完整复核：manifest 与三份产物全部校验通过才返回报告。"""
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or not isinstance(report, dict):
        return None
    if manifest.get("run_id") != run_id or report.get("run_id") != run_id:
        return None
    if manifest.get("status") != "complete":
        return None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    for name in EXPECTED_ARTIFACTS:
        meta = artifacts.get(name)
        path = directory / name
        if not isinstance(meta, dict) or not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if sha256_bytes(data) != meta.get("sha256") or len(data) != meta.get("bytes"):
            return None
    return report


def repair_runs(
    root: str | Path,
    *,
    mode: str,
    dry_run: bool = True,
    created_at: str | None = None,
) -> dict[str, Any]:
    """按显式权威语义修复审计发现的问题；默认 dry-run，按 run_id 幂等。

    ``manifest`` 模式：把校验通过的孤儿补登记进台账（恢复 manifest 落盘后、
    台账提交前退出的运行）；``ledger`` 模式：孤儿只报告不补登记。两种模式都
    不覆盖已存在的台账行，也不把损坏产物伪造成校验通过。
    """
    if mode not in REPAIR_MODES:
        raise FunnelCheckError(
            ErrorCode.USAGE,
            f"未知修复模式：{mode}",
            hint=f"可选：{', '.join(REPAIR_MODES)}",
        )
    root_path = Path(root)
    ledger_file = ledger_path(root_path)
    records, ledger_error = collect_records(root_path)
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for record in records:
        if record.status == STATUS_OK:
            continue
        if record.recoverable and mode == "manifest":
            actions.append(
                {
                    "action": "backfill_ledger",
                    "run_id": record.run_id,
                    "source": str(root_path / record.run_id / "report.json"),
                    "reason": "产物完整且校验通过，台账缺失；按 manifest 权威补登记",
                    "applied": False,
                }
            )
        elif record.recoverable:
            skipped.append(
                {
                    "run_id": record.run_id,
                    "status": record.status,
                    "reason": "ledger 权威模式：台账未提交即视为未完成，孤儿仅报告不补登记",
                }
            )
        else:
            skipped.append(
                {
                    "run_id": record.run_id,
                    "status": record.status,
                    "reason": "不覆盖已有台账、不伪造校验通过；需人工处理",
                }
            )
    if not dry_run:
        for action in actions:
            if action["action"] != "backfill_ledger":
                continue
            run_id = action["run_id"]
            if get_run(ledger_file, run_id) is not None:
                action["note"] = "台账已存在该 run_id，跳过（幂等）"
                continue
            report = _verified_report_for_backfill(root_path / run_id, run_id)
            if report is None:
                action["note"] = "应用前复核未通过，跳过"
                continue
            record_run(ledger_file, report, artifact_dir=run_id, created_at=created_at)
            action["applied"] = True
    return {
        "mode": mode,
        "dry_run": dry_run,
        "ledger": str(ledger_file),
        "ledger_error": ledger_error,
        "actions": actions,
        "skipped": skipped,
    }
