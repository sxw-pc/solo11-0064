"""本地 SQLite 运行台账：只保存审计摘要与产物相对路径。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import canonical_json
from .errors import ErrorCode, FunnelCheckError
from .timeutil import utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    title TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    semantics_version TEXT NOT NULL,
    entered_users INTEGER NOT NULL,
    converted_users INTEGER NOT NULL,
    overall_conversion REAL,
    cohort_size INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    artifact_dir TEXT NOT NULL,
    compute_mode TEXT NOT NULL DEFAULT 'full',
    contribution_sha256 TEXT,
    cache_reused_users INTEGER NOT NULL DEFAULT 0,
    cache_recomputed_users INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id TEXT PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    baseline_run_id TEXT NOT NULL,
    candidate_run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    changed_metric_count INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    artifact_dir TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at_utc);
CREATE INDEX IF NOT EXISTS idx_comparisons_created ON comparisons(created_at_utc);
"""

# 旧版本台账缺少增量贡献列，按添加式迁移补齐。
_RUN_EXTRA_COLUMNS = (
    ("compute_mode", "TEXT NOT NULL DEFAULT 'full'"),
    ("contribution_sha256", "TEXT"),
    ("cache_reused_users", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_recomputed_users", "INTEGER NOT NULL DEFAULT 0"),
)


def _db_error(exc: sqlite3.Error, message: str) -> FunnelCheckError:
    return FunnelCheckError(ErrorCode.IO, message, hint=str(exc))


def run_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": report.get("spec", {}).get("title"),
        "funnel": {
            "name": report.get("funnel", {}).get("name"),
            "entered_users": report.get("funnel", {}).get("entered_users"),
            "converted_users": report.get("funnel", {}).get("converted_users"),
            "overall_conversion": report.get("funnel", {}).get("overall_conversion"),
        },
        "retention": {
            "cohort_size": report.get("retention", {}).get("cohort_size"),
            "periods": report.get("retention", {}).get("periods", []),
        },
        "quality": {
            key: report.get("quality", {}).get("counts", {}).get(key)
            for key in (
                "records_seen",
                "included_events",
                "included_users",
                "duplicate_rows",
                "late_rows",
                "future_rows",
            )
        },
    }


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_ledger(path: str | Path) -> Path:
    ledger_path = Path(path)
    try:
        with _connect(ledger_path, read_only=False) as connection:
            connection.executescript(_SCHEMA)
            existing = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            for name, declaration in _RUN_EXTRA_COLUMNS:
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE runs ADD COLUMN {name} {declaration}"
                    )
            connection.commit()
    except sqlite3.Error as exc:
        raise _db_error(exc, f"无法初始化台账：{ledger_path}") from exc
    return ledger_path


def record_run(
    path: str | Path,
    report: dict[str, Any],
    *,
    artifact_dir: str,
    created_at: str | None = None,
    incremental: dict[str, Any] | None = None,
) -> None:
    ledger_path = init_ledger(path)
    summary = run_summary(report)
    compute_mode = "full"
    contribution_sha256 = None
    reused_users = 0
    recomputed_users = 0
    if incremental:
        compute_mode = incremental.get("mode", "incremental")
        contribution_sha256 = incremental.get("contribution_sha256")
        reused_users = int(incremental.get("reused_user_count", 0))
        recomputed_users = int(incremental.get("recomputed_user_count", 0))
    try:
        with _connect(ledger_path, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, created_at_utc, title, input_sha256, normalized_sha256,
                    spec_sha256, engine_version, semantics_version, entered_users,
                    converted_users, overall_conversion, cohort_size, summary_json,
                    artifact_dir, compute_mode, contribution_sha256,
                    cache_reused_users, cache_recomputed_users
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    created_at_utc=excluded.created_at_utc,
                    title=excluded.title,
                    input_sha256=excluded.input_sha256,
                    normalized_sha256=excluded.normalized_sha256,
                    spec_sha256=excluded.spec_sha256,
                    engine_version=excluded.engine_version,
                    semantics_version=excluded.semantics_version,
                    entered_users=excluded.entered_users,
                    converted_users=excluded.converted_users,
                    overall_conversion=excluded.overall_conversion,
                    cohort_size=excluded.cohort_size,
                    summary_json=excluded.summary_json,
                    artifact_dir=excluded.artifact_dir,
                    compute_mode=excluded.compute_mode,
                    contribution_sha256=excluded.contribution_sha256,
                    cache_reused_users=excluded.cache_reused_users,
                    cache_recomputed_users=excluded.cache_recomputed_users
                """,
                (
                    report["run_id"],
                    created_at or utc_now_iso(),
                    report.get("spec", {}).get("title", ""),
                    report.get("input", {}).get("sha256", ""),
                    report.get("input", {}).get("normalized_sha256", ""),
                    report.get("spec", {}).get("sha256", ""),
                    report.get("engine", {}).get("version", ""),
                    report.get("engine", {}).get("semantics_version", ""),
                    report.get("funnel", {}).get("entered_users", 0),
                    report.get("funnel", {}).get("converted_users", 0),
                    report.get("funnel", {}).get("overall_conversion"),
                    report.get("retention", {}).get("cohort_size", 0),
                    canonical_json(summary),
                    artifact_dir,
                    compute_mode,
                    contribution_sha256,
                    reused_users,
                    recomputed_users,
                ),
            )
            connection.commit()
    except (sqlite3.Error, KeyError) as exc:
        raise _db_error(exc, f"无法写入运行台账：{ledger_path}") from exc


def record_comparison(
    path: str | Path,
    comparison: dict[str, Any],
    *,
    artifact_dir: str,
    created_at: str | None = None,
) -> None:
    ledger_path = init_ledger(path)
    flags = comparison.get("flags", {})
    summary = {
        "verdict": comparison.get("verdict"),
        "flags": flags,
        "baseline": comparison.get("baseline", {}),
        "candidate": comparison.get("candidate", {}),
        "changed_metric_count": comparison.get("changed_metric_count", 0),
    }
    try:
        with _connect(ledger_path, read_only=False) as connection:
            connection.execute(
                """
                INSERT INTO comparisons (
                    comparison_id, created_at_utc, baseline_run_id, candidate_run_id,
                    verdict, flags_json, changed_metric_count, summary_json, artifact_dir
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(comparison_id) DO UPDATE SET
                    created_at_utc=excluded.created_at_utc,
                    baseline_run_id=excluded.baseline_run_id,
                    candidate_run_id=excluded.candidate_run_id,
                    verdict=excluded.verdict,
                    flags_json=excluded.flags_json,
                    changed_metric_count=excluded.changed_metric_count,
                    summary_json=excluded.summary_json,
                    artifact_dir=excluded.artifact_dir
                """,
                (
                    comparison["comparison_id"],
                    created_at or utc_now_iso(),
                    comparison["baseline"]["run_id"],
                    comparison["candidate"]["run_id"],
                    comparison.get("verdict", ""),
                    canonical_json(flags),
                    comparison.get("changed_metric_count", 0),
                    canonical_json(summary),
                    artifact_dir,
                ),
            )
            connection.commit()
    except (sqlite3.Error, KeyError) as exc:
        raise _db_error(exc, f"无法写入对比台账：{ledger_path}") from exc


def _rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with _connect(path, read_only=True) as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def list_runs(path: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    rows = _rows(
        Path(path),
        "SELECT * FROM runs ORDER BY created_at_utc DESC, run_id DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["summary"] = json.loads(row.pop("summary_json"))
    return rows


def get_run(path: str | Path, run_id: str) -> dict[str, Any] | None:
    rows = _rows(Path(path), "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if not rows:
        return None
    row = rows[0]
    row["summary"] = json.loads(row.pop("summary_json"))
    return row


def list_comparisons(path: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    rows = _rows(
        Path(path),
        "SELECT * FROM comparisons ORDER BY created_at_utc DESC, comparison_id DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["summary"] = json.loads(row.pop("summary_json"))
        row["flags"] = json.loads(row.pop("flags_json"))
    return rows


def get_comparison(path: str | Path, comparison_id: str) -> dict[str, Any] | None:
    rows = _rows(
        Path(path), "SELECT * FROM comparisons WHERE comparison_id = ?", (comparison_id,)
    )
    if not rows:
        return None
    row = rows[0]
    row["summary"] = json.loads(row.pop("summary_json"))
    row["flags"] = json.loads(row.pop("flags_json"))
    return row
