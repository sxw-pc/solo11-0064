"""运行期常量、确定性与输入限制设置。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ENGINE_VERSION = "1.0.0"
SEMANTICS_VERSION = "2026.11.1"
RUN_SCHEMA_VERSION = 1
COMPARE_SCHEMA_VERSION = 1

MAX_EVENT_BYTES = 20 * 1024 * 1024
MAX_SPEC_BYTES = 1 * 1024 * 1024
MAX_QUALITY_SAMPLES = 8
DEFAULT_RUNS_DIR = "runs"

_DURATION_RE = re.compile(r"^\s*(?P<amount>\d+)\s*(?P<unit>[smhdw])\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def parse_duration(
    value: str, *, field: str = "duration", allow_zero: bool = False
) -> int:
    """把 30s/15m/24h/7d/2w 转为秒；按需允许零值。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是持续时间字符串，例如 7d")
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"{field} 格式无效：{value!r}（支持 s/m/h/d/w）")
    seconds = int(match.group("amount")) * _DURATION_UNITS[match.group("unit")]
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ValueError(f"{field} 必须大于 0" if not allow_zero else f"{field} 不能为负")
    return seconds


def canonical_json(value: Any) -> str:
    """稳定 JSON 文本：键排序、无多余空白，用于哈希与比较。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def make_run_id(spec_sha256: str, events_sha256: str) -> str:
    material = f"funnelcheck|{SEMANTICS_VERSION}|{spec_sha256}|{events_sha256}"
    return f"run-{sha256_text(material)[:20]}"


def make_comparison_id(baseline_run_id: str, candidate_run_id: str) -> str:
    material = f"compare|{SEMANTICS_VERSION}|{baseline_run_id}|{candidate_run_id}"
    return f"cmp-{sha256_text(material)[:20]}"


def safe_slug(value: str, *, fallback: str = "item", limit: int = 64) -> str:
    slug = _SLUG_RE.sub("-", value).strip("-._")
    return (slug or fallback)[:limit]


def clip_message(message: str, limit: int = 240) -> str:
    clean = " ".join(str(message).split())
    return clean[:limit] + ("…" if len(clean) > limit else "")
