"""单次运行与两次运行对比的编排层。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_RUNS_DIR,
    MAX_EVENT_BYTES,
    make_comparison_id,
    make_run_id,
)
from .engine.compare import compare_reports
from .engine.funnel import compute_funnel
from .engine.retention import compute_retention
from .errors import ErrorCode, FunnelCheckError, NotFoundError
from .ledger import record_comparison, record_run
from .loader import load_events
from .models import CompareOutcome, RunOutcome
from .normalize import normalize_batch
from .report import (
    build_run_report,
    read_json,
    write_comparison_artifacts,
    write_run_artifacts,
)
from .spec import load_spec

RUN_ID_RE = re.compile(r"^run-[0-9a-f]{20}$")
COMPARISON_ID_RE = re.compile(r"^cmp-[0-9a-f]{20}$")


def runs_root(value: str | Path | None = None) -> Path:
    return Path(value or DEFAULT_RUNS_DIR)


def ledger_path(root: str | Path) -> Path:
    return Path(root) / "ledger.sqlite"


def run_dir(root: str | Path, run_id: str) -> Path:
    if not RUN_ID_RE.match(run_id):
        raise NotFoundError(f"运行 ID 格式无效：{run_id}")
    directory = Path(root) / run_id
    _ensure_within(Path(root), directory)
    return directory


def comparison_dir(root: str | Path, comparison_id: str) -> Path:
    if not COMPARISON_ID_RE.match(comparison_id):
        raise NotFoundError(f"对比 ID 格式无效：{comparison_id}")
    directory = Path(root) / "comparisons" / comparison_id
    _ensure_within(Path(root), directory)
    return directory


def _ensure_within(root: Path, candidate: Path) -> None:
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except OSError as exc:
        raise FunnelCheckError(ErrorCode.IO, "无法解析本地路径", hint=str(exc)) from exc
    if resolved_root != resolved_candidate and resolved_root not in resolved_candidate.parents:
        raise NotFoundError("拒绝访问运行目录之外的路径")


def load_run_report(root: str | Path, run_id: str) -> dict[str, Any]:
    directory = run_dir(root, run_id)
    report_path = directory / "report.json"
    if not report_path.is_file():
        raise NotFoundError(f"找不到运行产物：{run_id}", hint="请先执行 run 命令。")
    report = read_json(report_path)
    if report.get("run_id") != run_id:
        raise FunnelCheckError(
            ErrorCode.ARTIFACT_MISSING, f"运行产物中的 run_id 不匹配：{run_id}"
        )
    return report


def load_comparison(root: str | Path, comparison_id: str) -> dict[str, Any]:
    directory = comparison_dir(root, comparison_id)
    path = directory / "compare.json"
    if not path.is_file():
        raise NotFoundError(f"找不到对比产物：{comparison_id}")
    comparison = read_json(path)
    if comparison.get("comparison_id") != comparison_id:
        raise FunnelCheckError(
            ErrorCode.ARTIFACT_MISSING, f"对比产物中的 ID 不匹配：{comparison_id}"
        )
    return comparison


def execute_run(
    events_path: str | Path,
    spec_path: str | Path,
    root: str | Path | None = None,
    *,
    max_event_bytes: int = MAX_EVENT_BYTES,
    write_ledger: bool = True,
    created_at: str | None = None,
) -> RunOutcome:
    root_path = runs_root(root)
    spec_bundle = load_spec(spec_path)
    raw = load_events(events_path, max_bytes=max_event_bytes)
    normalized = normalize_batch(raw, spec_bundle.spec)
    funnel = compute_funnel(normalized.events, spec_bundle.spec)
    retention = compute_retention(normalized.events, spec_bundle.spec)
    run_id = make_run_id(spec_bundle.sha256, raw.sha256)
    report = build_run_report(
        run_id=run_id,
        spec_bundle=spec_bundle,
        raw=raw,
        normalized=normalized,
        funnel=funnel,
        retention=retention,
        events_file_name=Path(events_path).name,
    )
    directory = run_dir(root_path, run_id)
    written = write_run_artifacts(report, directory)
    if write_ledger:
        record_run(
            ledger_path(root_path),
            report,
            artifact_dir=run_id,
            created_at=created_at,
        )
    return RunOutcome(
        run_id=run_id,
        artifact_dir=directory,
        report=report,
        written=written,
    )


def execute_comparison(
    baseline_run_id: str,
    candidate_run_id: str,
    root: str | Path | None = None,
    *,
    write_ledger: bool = True,
    created_at: str | None = None,
) -> CompareOutcome:
    root_path = runs_root(root)
    baseline = load_run_report(root_path, baseline_run_id)
    candidate = load_run_report(root_path, candidate_run_id)
    comparison = compare_reports(baseline, candidate)
    comparison_id = make_comparison_id(baseline_run_id, candidate_run_id)
    comparison["comparison_id"] = comparison_id
    directory = comparison_dir(root_path, comparison_id)
    written = write_comparison_artifacts(comparison, directory)
    if write_ledger:
        record_comparison(
            ledger_path(root_path),
            comparison,
            artifact_dir=f"comparisons/{comparison_id}",
            created_at=created_at,
        )
    return CompareOutcome(
        comparison_id=comparison_id,
        artifact_dir=directory,
        comparison=comparison,
        written=written,
    )
