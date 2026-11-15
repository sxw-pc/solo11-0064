"""运行报告的组装、稳定序列化与原子落盘。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import (
    ENGINE_VERSION,
    RUN_SCHEMA_VERSION,
    SEMANTICS_VERSION,
    sha256_bytes,
)
from .errors import ErrorCode, FunnelCheckError
from .models import NormalizedBatch, RawInput, SpecBundle, format_duration_seconds


def duration_rows(funnel: dict[str, Any]) -> list[dict[str, Any]]:
    """把逐步耗时结果规整为渲染层可消费的行。

    保留 ``None``（JSON ``null``）语义：无法估计的中位耗时不得被零或
    完成者分位数替代。Markdown/HTML 与 JSON 共用这一份结构。
    """
    duration = funnel.get("duration") or {}
    rows: list[dict[str, Any]] = []
    for transition in duration.get("transitions", []):
        survival = transition.get("survival") or {}
        rows.append(
            {
                "from_step": transition.get("from_step"),
                "to_step": transition.get("to_step"),
                "from_name": transition.get("from_name"),
                "to_name": transition.get("to_name"),
                "from_event": transition.get("from_event"),
                "to_event": transition.get("to_event"),
                "entered_users": transition.get("entered_users", 0),
                "converted_users": transition.get("converted_users", 0),
                "censored_users": transition.get("censored_users", 0),
                "censored_window_users": transition.get("censored_window_users", 0),
                "censored_observation_users": transition.get(
                    "censored_observation_users", 0
                ),
                "conversion_rate": transition.get("conversion_rate"),
                "observed_median_seconds": transition.get("observed_median_seconds"),
                "observed_median_label": transition.get("observed_median_label"),
                "survival_median_seconds": survival.get("median_seconds"),
                "survival_median_label": survival.get("median_label"),
                "points": [
                    {
                        "label": point.get("label"),
                        "elapsed_seconds": point.get("elapsed_seconds"),
                        "fraction": point.get("fraction"),
                        "window_fraction": point.get("window_fraction"),
                        "at_risk_users": point.get("at_risk_users", 0),
                        "converted_by": point.get("converted_by", 0),
                        "censored_by": point.get("censored_by", 0),
                        "not_converted_ratio": point.get("not_converted_ratio"),
                    }
                    for point in survival.get("points", [])
                ],
            }
        )
    return rows


def duration_median_label(value: Any) -> str:
    """无法估计时明确输出 ``null``，与 JSON 语义一致。"""
    return "null" if value is None else format_duration_seconds(value)


def dump_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise FunnelCheckError(
            ErrorCode.IO, f"无法写入产物：{path}", hint=str(exc)
        ) from exc


def build_run_report(
    *,
    run_id: str,
    spec_bundle: SpecBundle,
    raw: RawInput,
    normalized: NormalizedBatch,
    funnel: dict[str, Any],
    retention: dict[str, Any],
    events_file_name: str,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "engine": {
            "version": ENGINE_VERSION,
            "semantics_version": SEMANTICS_VERSION,
        },
        "spec": {
            "title": spec_bundle.spec.title,
            "sha256": spec_bundle.sha256,
            "byte_count": spec_bundle.byte_count,
            "document": spec_bundle.document,
        },
        "input": {
            "file_name": events_file_name,
            "byte_count": raw.byte_count,
            "line_count": raw.line_count,
            "sha256": raw.sha256,
            "normalized_sha256": normalized.normalized_sha256,
        },
        "funnel": funnel,
        "retention": retention,
        "quality": normalized.quality,
    }


def _artifact_manifest(report: dict[str, Any], artifacts: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": report["run_id"],
        "status": "complete",
        "engine": report["engine"],
        "input_sha256": report["input"]["sha256"],
        "normalized_sha256": report["input"]["normalized_sha256"],
        "spec_sha256": report["spec"]["sha256"],
        "artifacts": artifacts,
    }


def _hashed_artifacts(run_dir: Path, names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        path = run_dir / name
        data = path.read_bytes()
        result[name] = {"sha256": sha256_bytes(data), "bytes": len(data)}
    return result


def write_run_artifacts(report: dict[str, Any], run_dir: str | Path) -> tuple[str, ...]:
    from .render_html import render_run_html
    from .render_markdown import render_run_markdown

    directory = Path(run_dir)
    files = {
        "report.json": dump_json(report),
        "report.md": render_run_markdown(report),
        "report.html": render_run_html(report),
    }
    for name, content in files.items():
        write_text_atomic(directory / name, content)
    manifest = _artifact_manifest(report, _hashed_artifacts(directory, list(files)))
    write_text_atomic(directory / "manifest.json", dump_json(manifest))
    return tuple(sorted([*files, "manifest.json"]))


def _comparison_manifest(
    comparison: dict[str, Any], artifacts: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "comparison_id": comparison["comparison_id"],
        "status": "complete",
        "baseline_run_id": comparison["baseline"]["run_id"],
        "candidate_run_id": comparison["candidate"]["run_id"],
        "verdict": comparison["verdict"],
        "artifacts": artifacts,
    }


def write_comparison_artifacts(
    comparison: dict[str, Any], comparison_dir: str | Path
) -> tuple[str, ...]:
    from .render_html import render_compare_html
    from .render_markdown import render_compare_markdown

    directory = Path(comparison_dir)
    files = {
        "compare.json": dump_json(comparison),
        "compare.md": render_compare_markdown(comparison),
        "compare.html": render_compare_html(comparison),
    }
    for name, content in files.items():
        write_text_atomic(directory / name, content)
    manifest = _comparison_manifest(
        comparison, _hashed_artifacts(directory, list(files))
    )
    write_text_atomic(directory / "manifest.json", dump_json(manifest))
    return tuple(sorted([*files, "manifest.json"]))


def read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FunnelCheckError(
            ErrorCode.ARTIFACT_MISSING, f"无法读取产物：{source}", hint=str(exc)
        ) from exc
    except json.JSONDecodeError as exc:
        raise FunnelCheckError(
            ErrorCode.ARTIFACT_MISSING, f"产物不是有效 JSON：{source}", hint=str(exc)
        ) from exc
    if not isinstance(payload, dict):
        raise FunnelCheckError(
            ErrorCode.ARTIFACT_MISSING, f"产物根节点不是对象：{source}"
        )
    return payload
