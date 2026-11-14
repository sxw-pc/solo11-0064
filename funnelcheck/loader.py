"""JSONL 事件输入加载：大小限制、逐行解析与原始哈希。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import MAX_EVENT_BYTES, sha256_bytes
from .errors import ErrorCode, FunnelCheckError
from .models import RawInput, RawRow


def _reject_constant(value: str) -> None:
    raise ValueError(f"不支持的 JSON 常量：{value}")


def _parse_line(line: str) -> dict[str, Any]:
    payload = json.loads(line, parse_constant=_reject_constant)
    if not isinstance(payload, dict):
        raise TypeError("事件必须是 JSON 对象")
    return payload


def load_events(path: str | Path, *, max_bytes: int = MAX_EVENT_BYTES) -> RawInput:
    event_path = Path(path)
    try:
        stat = event_path.stat()
    except OSError as exc:
        raise FunnelCheckError(
            ErrorCode.IO, f"无法读取事件文件：{event_path}", hint=str(exc)
        ) from exc
    if not event_path.is_file():
        raise FunnelCheckError(ErrorCode.IO, f"事件路径不是文件：{event_path}")
    if stat.st_size > max_bytes:
        raise FunnelCheckError(
            ErrorCode.INPUT_TOO_LARGE,
            f"事件文件为 {stat.st_size} 字节，超过 {max_bytes} 字节限制",
            hint="请先在本地拆分或压缩输入；本工具不做静默抽样。",
        )
    try:
        raw = event_path.read_bytes()
    except OSError as exc:
        raise FunnelCheckError(
            ErrorCode.IO, f"无法读取事件文件：{event_path}", hint=str(exc)
        ) from exc
    if len(raw) > max_bytes:
        raise FunnelCheckError(
            ErrorCode.INPUT_TOO_LARGE,
            f"事件文件超过 {max_bytes} 字节限制",
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FunnelCheckError(
            ErrorCode.INPUT_ENCODING,
            "事件文件不是有效 UTF-8",
            hint=f"字节偏移 {exc.start} 附近无法解码。",
        ) from exc

    rows: list[RawRow] = []
    blank_lines = 0
    line_count = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        line_count += 1
        if not line.strip():
            blank_lines += 1
            continue
        try:
            payload = _parse_line(line)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            rows.append(RawRow(line_no=line_no, payload=None, error=str(exc)))
            continue
        rows.append(RawRow(line_no=line_no, payload=payload))
    return RawInput(
        rows=tuple(rows),
        byte_count=len(raw),
        sha256=sha256_bytes(raw),
        line_count=line_count,
        blank_lines=blank_lines,
    )
