from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReadPlanItem:
    path: str
    reason: str
    closes_gap: str
    priority: int
    max_ranges: int = 1

    def summary(self) -> str:
        range_word = "range" if self.max_ranges == 1 else "ranges"
        return f"{self.path} ({self.reason}; up to {self.max_ranges} {range_word})"


@dataclass
class ReadPlan:
    task_id: str
    subsystem: str
    items: list[ReadPlanItem]
    stop_condition: str
    approved: bool = False

    def allowed_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.items if item.path)

    def item_for_path(self, path: str) -> ReadPlanItem | None:
        for item in self.items:
            if item.path == path:
                return item
        return None

    def summary(self, max_items: int = 4) -> str:
        if not self.items:
            return "(no items)"
        pieces = [item.summary() for item in self.items[:max_items]]
        if len(self.items) > max_items:
            pieces.append(f"... and {len(self.items) - max_items} more")
        return "; ".join(pieces)


def _extract_json_object(text: str) -> dict:
    """Extract the first JSON object from text that may contain prose around it."""
    text = text.strip()
    # Try direct parse first
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    # Try json.loads starting from each '{' in the text
    search_start = 0
    while True:
        idx = text.find("{", search_start)
        if idx == -1:
            break
        try:
            decoder = json.JSONDecoder()
            payload, end = decoder.raw_decode(text, idx)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        search_start = idx + 1
    raise ValueError("no valid JSON object found in message")


def parse_read_plan_message(message: str, *, task_id: str) -> ReadPlan:
    payload = _extract_json_object(message)
    if not isinstance(payload, dict):
        raise ValueError("read plan payload must be a JSON object")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("read plan must include a non-empty items list")

    items: list[ReadPlanItem] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"read plan item {index} must be an object")
        path = str(item.get("path", "")).strip()
        reason = str(item.get("reason", "")).strip()
        closes_gap = str(item.get("closes_gap", "")).strip()
        priority = int(item.get("priority", index))
        max_ranges = int(item.get("max_ranges", 1))
        if not path:
            raise ValueError(f"read plan item {index} is missing path")
        if not reason:
            raise ValueError(f"read plan item {index} is missing reason")
        if not closes_gap:
            raise ValueError(f"read plan item {index} is missing closes_gap")
        items.append(
            ReadPlanItem(
                path=path,
                reason=reason,
                closes_gap=closes_gap,
                priority=priority,
                max_ranges=max(1, max_ranges),
            )
        )

    subsystem = str(payload.get("subsystem", "")).strip() or "broad-analysis"
    stop_condition = str(payload.get("stop_condition", "")).strip()
    if not stop_condition:
        raise ValueError("read plan must include stop_condition")

    plan = ReadPlan(
        task_id=task_id,
        subsystem=subsystem,
        items=sorted(items, key=lambda item: (item.priority, item.path)),
        stop_condition=stop_condition,
        approved=True,
    )
    return plan


def read_plan_to_dict(plan: ReadPlan) -> dict[str, Any]:
    return {
        "task_id": plan.task_id,
        "subsystem": plan.subsystem,
        "items": [
            {
                "path": item.path,
                "reason": item.reason,
                "closes_gap": item.closes_gap,
                "priority": item.priority,
                "max_ranges": item.max_ranges,
            }
            for item in plan.items
        ],
        "stop_condition": plan.stop_condition,
        "approved": plan.approved,
    }
