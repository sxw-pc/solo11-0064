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
from .models import NormalizedBatch, RawInput, SpecBundle
from .render_html import render_compare_html, render_run_html
from .render_markdown import render_compare_markdown, render_run_markdown


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
