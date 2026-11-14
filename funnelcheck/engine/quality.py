"""数据质量计数与可审计样例。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import MAX_QUALITY_SAMPLES

COUNTER_NAMES = (
    "records_seen",
    "records_parsed",
    "blank_lines",
    "malformed_json",
    "missing_required",
    "invalid_timestamp",
    "ambiguous_local_time",
    "invalid_properties",
    "duplicate_rows",
    "duplicate_groups",
    "future_rows",
    "future_within_grace",
    "late_rows",
    "late_included",
    "late_excluded",
    "negative_latency",
    "out_of_order",
    "excluded_events",
    "included_events",
    "included_users",
)


@dataclass
class QualityTracker:
    sample_limit: int = MAX_QUALITY_SAMPLES
    counts: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in COUNTER_NAMES}
    )
    samples: list[dict[str, Any]] = field(default_factory=list)

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self.counts:
            raise KeyError(name)
        if amount < 0:
            raise ValueError("quality count cannot be negative")
        self.counts[name] += amount

    def sample(
        self,
        reason: str,
        *,
        line_no: int,
        event_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        if len(self.samples) >= self.sample_limit:
            return
        item: dict[str, Any] = {"line": line_no, "reason": reason}
        if event_id:
            item["event_id"] = event_id
        if detail:
            item["detail"] = detail
        self.samples.append(item)

    def finalize(self, rules: dict[str, Any]) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "samples": list(self.samples),
            "rules": rules,
        }
