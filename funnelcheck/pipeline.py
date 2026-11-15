"""单次运行与两次运行对比的编排层。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import incremental
from .config import (
    DEFAULT_RUNS_DIR,
    MAX_EVENT_BYTES,
    SEMANTICS_VERSION,
    make_comparison_id,
    make_run_id,
    sha256_bytes,
)
from .engine.compare import compare_reports
from .errors import ErrorCode, FunnelCheckError, NotFoundError
from .ledger import record_comparison, record_run
from .loader import load_events
from .models import CompareOutcome, RunOutcome
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


def _full_compute(
    raw,
    spec_bundle,
    run_id,
    root_path,
    *,
    max_event_bytes,
    write_ledger,
    created_at,
    events_path,
    use_incremental_cache,
) -> tuple[RunOutcome, dict[str, Any], Any]:
    """全量正确路径：重算所有用户，并刷新同规格增量缓存。"""
    normalized, groups = incremental.normalize_with_groups(raw, spec_bundle.spec)
    funnel_contrib, retention_contrib = incremental.full_contributions(
        normalized.events, spec_bundle.spec
    )
    funnel, retention = incremental.aggregate(
        funnel_contrib, retention_contrib, spec_bundle.spec
    )
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
    contribution_sha = incremental.contribution_set_sha256(
        funnel_contrib, retention_contrib
    )
    incremental_meta = {
        "mode": "full",
        "semantics_version": SEMANTICS_VERSION,
        "spec_sha256": spec_bundle.sha256,
        "input_sha256": raw.sha256,
        "reused_user_count": 0,
        "recomputed_user_count": len(groups_user_ids(groups)),
        "contribution_sha256": contribution_sha,
    }
    written = write_run_artifacts(report, directory, incremental=incremental_meta)
    manifest_sha = _manifest_hash(directory)
    if write_ledger:
        record_run(
            ledger_path(root_path),
            report,
            artifact_dir=run_id,
            created_at=created_at,
            incremental=incremental_meta,
        )
    state = incremental.build_state(
        spec_bundle=spec_bundle,
        raw=raw,
        run_id=run_id,
        groups=groups,
        funnel=funnel_contrib,
        retention=retention_contrib,
        manifest_sha256=manifest_sha,
    )
    if use_incremental_cache:
        _commit_cache(root_path, spec_bundle, state)
    outcome = RunOutcome(
        run_id=run_id,
        artifact_dir=directory,
        report=report,
        written=written,
        compute_mode="full",
        incremental=incremental_meta,
    )
    return outcome, state, groups


def groups_user_ids(groups: dict[str, list[dict[str, Any]]]) -> set[str]:
    return {
        str(entry["user_id"])
        for entries in groups.values()
        for entry in entries
    }


def _manifest_hash(directory: Path) -> str:
    return sha256_bytes((directory / "manifest.json").read_bytes())


def _commit_cache(root_path: Path, spec_bundle, state: dict[str, Any]) -> None:
    cache_path = incremental.cache_slot_path(root_path, spec_bundle)
    try:
        incremental.write_state_atomic(cache_path, state)
    except OSError:
        # 缓存不可写不应阻断运行本身；下次运行自动走全量。
        pass


def _incremental_compute(
    raw,
    spec_bundle,
    run_id,
    root_path,
    state,
    *,
    write_ledger,
    created_at,
    events_path,
    use_incremental_cache,
):
    """增量正确路径：只重算受影响用户；前提无法证明时返回 None 触发全量。"""
    normalized, groups = incremental.normalize_with_groups(raw, spec_bundle.spec)
    plan = incremental.plan_incremental(state, groups, spec_bundle.spec)
    if plan is None:
        return None
    try:
        funnel_contrib, retention_contrib, stats = incremental.incremental_contributions(
            normalized.events, spec_bundle.spec, state, plan
        )
    except incremental.CacheProofError:
        return None
    funnel, retention = incremental.aggregate(
        funnel_contrib, retention_contrib, spec_bundle.spec
    )
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
    contribution_sha = incremental.contribution_set_sha256(funnel_contrib, retention_contrib)
    incremental_meta = {
        "mode": "incremental",
        "semantics_version": SEMANTICS_VERSION,
        "spec_sha256": spec_bundle.sha256,
        "input_sha256": raw.sha256,
        "reused_user_count": stats["reused_user_count"],
        "recomputed_user_count": stats["recomputed_user_count"],
        "affected_group_count": plan.changed_group_count,
        "contribution_sha256": contribution_sha,
    }
    written = write_run_artifacts(report, directory, incremental=incremental_meta)
    manifest_sha = _manifest_hash(directory)
    if write_ledger:
        record_run(
            ledger_path(root_path),
            report,
            artifact_dir=run_id,
            created_at=created_at,
            incremental=incremental_meta,
        )
    new_state = incremental.build_state(
        spec_bundle=spec_bundle,
        raw=raw,
        run_id=run_id,
        groups=groups,
        funnel=funnel_contrib,
        retention=retention_contrib,
        manifest_sha256=manifest_sha,
    )
    if use_incremental_cache:
        _commit_cache(root_path, spec_bundle, new_state)
    return RunOutcome(
        run_id=run_id,
        artifact_dir=directory,
        report=report,
        written=written,
        compute_mode="incremental",
        incremental=incremental_meta,
    )


def execute_run(
    events_path: str | Path,
    spec_path: str | Path,
    root: str | Path | None = None,
    *,
    max_event_bytes: int = MAX_EVENT_BYTES,
    write_ledger: bool = True,
    created_at: str | None = None,
    use_incremental_cache: bool = True,
) -> RunOutcome:
    root_path = runs_root(root)
    spec_bundle = load_spec(spec_path)
    raw = load_events(events_path, max_bytes=max_event_bytes)
    run_id = make_run_id(spec_bundle.sha256, raw.sha256)
    ledger_file = ledger_path(root_path) if write_ledger else None

    if use_incremental_cache:
        incremental.discard_staging_files(root_path)
        cache_path = incremental.cache_slot_path(root_path, spec_bundle)
        state = incremental.load_reusable_state(cache_path, spec_bundle)
        if state is not None and state.get("input") == incremental.input_digest(raw):
            # 同输入重跑：输入摘要、贡献 manifest、ledger run_id 必须全部一致。
            chain_ok = incremental.verify_chain(
                root_path, state, ledger_file=ledger_file
            )
            if chain_ok:
                directory = run_dir(root_path, run_id)
                if (directory / "report.json").is_file():
                    return _load_existing_outcome(directory, state)
        elif state is not None:
            chain_ok = incremental.verify_chain(
                root_path, state, ledger_file=ledger_file
            )
            if chain_ok:
                outcome = _incremental_compute(
                    raw,
                    spec_bundle,
                    run_id,
                    root_path,
                    state,
                    write_ledger=write_ledger,
                    created_at=created_at,
                    events_path=events_path,
                    use_incremental_cache=use_incremental_cache,
                )
                if outcome is not None:
                    return outcome
            # 链断裂或无法证明兼容：安全降级到全量。

    outcome, _state, _groups = _full_compute(
        raw,
        spec_bundle,
        run_id,
        root_path,
        max_event_bytes=max_event_bytes,
        write_ledger=write_ledger,
        created_at=created_at,
        events_path=events_path,
        use_incremental_cache=use_incremental_cache,
    )
    return outcome


def _load_existing_outcome(directory: Path, state: dict[str, Any]) -> RunOutcome:
    report = read_json(directory / "report.json")
    manifest = read_json(directory / "manifest.json")
    incremental_meta = manifest.get("incremental")
    return RunOutcome(
        run_id=report["run_id"],
        artifact_dir=directory,
        report=report,
        written=tuple(sorted(name for name in ("report.json", "report.md", "report.html", "manifest.json") if (directory / name).is_file())),
        compute_mode=incremental_meta.get("mode", "full") if incremental_meta else "full",
        incremental=incremental_meta,
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
