"""测试用规格、事件与运行构建助手。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from funnelcheck.loader import load_events
from funnelcheck.models import NormalizedBatch, RunOutcome
from funnelcheck.normalize import normalize_batch
from funnelcheck.pipeline import execute_run
from funnelcheck.spec import load_spec

DEFAULT_SPEC: dict[str, Any] = {
    "spec_version": 1,
    "title": "测试分析",
    "analysis": {
        "timezone": "UTC",
        "as_of": "2026-03-20T00:00:00Z",
        "future_grace": "0h",
        "late_after": "48h",
        "late_policy": "exclude",
    },
    "events": {
        "dedup_key": "event_id",
        "dedup_keep": "first_received",
        "same_timestamp_order": "received_then_id",
    },
    "funnel": {
        "name": "view_to_buy",
        "window": "7d",
        "steps": [
            {"name": "浏览", "event": "view"},
            {"name": "注册", "event": "signup"},
            {"name": "购买", "event": "buy"},
        ],
    },
    "retention": {
        "cohort_event": "signup",
        "return_event": "view",
        "unit": "day",
        "periods": [1, 7],
        "mode": "exact",
    },
    "segments": [],
}


def _deep_update(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)


def spec_document(**overrides: Any) -> dict[str, Any]:
    data = deepcopy(DEFAULT_SPEC)
    _deep_update(data, overrides)
    return data


def write_spec(
    directory: Path,
    *,
    name: str = "spec.yaml",
    **overrides: Any,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        yaml.safe_dump(spec_document(**overrides), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def write_raw_spec(directory: Path, text: str, *, name: str = "spec.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def make_event(
    event_id: str,
    user_id: str,
    event_name: str,
    occurred_at: str,
    received_at: str | None = None,
    **attributes: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": event_id,
        "user_id": user_id,
        "event_name": event_name,
        "occurred_at": occurred_at,
        "received_at": received_at or occurred_at,
    }
    event.update(attributes)
    return event


def write_events(
    directory: Path,
    events: list[dict[str, Any]],
    *,
    name: str = "events.jsonl",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        encoding="utf-8",
        newline="\n",
    )
    return path


def normalize_case(
    directory: Path,
    rows: list[dict[str, Any]],
    *,
    name: str = "case",
    **spec_overrides: Any,
) -> NormalizedBatch:
    case_dir = directory / name
    spec_path = write_spec(case_dir, **spec_overrides)
    event_path = write_events(case_dir, rows)
    return normalize_batch(load_events(event_path), load_spec(spec_path).spec)


def run_once(
    directory: Path,
    rows: list[dict[str, Any]],
    *,
    name: str = "case",
    runs_dir: Path | None = None,
    **spec_overrides: Any,
) -> RunOutcome:
    case_dir = directory / name
    spec_path = write_spec(case_dir, **spec_overrides)
    event_path = write_events(case_dir, rows)
    return execute_run(
        event_path,
        spec_path,
        runs_dir or (directory / "runs"),
        write_ledger=False,
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def simple_funnel_events() -> list[dict[str, Any]]:
    return [
        make_event("a1", "u-a", "view", "2026-03-01T10:00:00Z"),
        make_event("a2", "u-a", "signup", "2026-03-01T10:01:00Z"),
        make_event("a3", "u-a", "buy", "2026-03-01T10:02:00Z"),
        make_event("b1", "u-b", "view", "2026-03-01T11:00:00Z"),
        make_event("b2", "u-b", "signup", "2026-03-01T11:01:00Z"),
        make_event("c1", "u-c", "view", "2026-03-01T12:00:00Z"),
    ]
