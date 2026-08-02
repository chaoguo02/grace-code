"""core/resource_metrics.py

资源指标收集器 — 聚合 ResourceGovernor 快照，导出给 observability 层。

Phase 0: 被动收集（外部调用 collect()），不启动后台线程。
Phase 3+: 可切换为周期性自动收集。
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from typing import Any

from core.resource_governor import (
    ResourceGovernor,
    ResourceGovernorSnapshot,
    ResourceKind,
    ResourcePressure,
)

logger = logging.getLogger(__name__)


@dataclass
class ResourceMetricsCollector:
    """收集并导出资源指标。

    Usage:
        collector = ResourceMetricsCollector(governor)
        snap = collector.collect()       # take a snapshot
        metrics = collector.export()     # export for observability
    """

    governor: ResourceGovernor
    snapshot_history: list[ResourceGovernorSnapshot] = field(default_factory=list)
    max_history: int = 128

    def collect(self) -> ResourceGovernorSnapshot:
        """采集一次快照，追加到历史记录。"""
        snap = self.governor.snapshot()
        self.snapshot_history.append(snap)
        if len(self.snapshot_history) > self.max_history:
            self.snapshot_history = self.snapshot_history[-self.max_history:]
        return snap

    def latest(self) -> ResourceGovernorSnapshot | None:
        """返回最近一次快照，无数据时返回 None。"""
        return self.snapshot_history[-1] if self.snapshot_history else None

    def export(self) -> dict[str, Any]:
        """导出当前指标为 dict。

        格式兼容 observability 层（tracing.py）的 metadata 结构。
        """
        snap = self.latest()
        if snap is None:
            return {
                "mode": self.governor.mode,
                "snapshots": [],
                "blocked_would_be": {},
            }

        snapshot_list: list[dict[str, Any]] = []
        for kind, ks in snap.snapshots.items():
            snapshot_list.append({
                "kind": kind.name,
                "limit": ks.limit,
                "consumed": ks.consumed,
                "reserved": ks.reserved,
                "available": ks.available,
                "queued": ks.queued,
                "queue_wait_max_s": ks.queue_wait_max_s,
                "pressure": ks.pressure.name,
                "utilization_pct": round(
                    ((ks.consumed + ks.reserved) / max(1, ks.limit)) * 100, 1,
                ),
            })

        return {
            "mode": snap.mode,
            "timestamp_s": snap.timestamp_s,
            "total_grants": snap.total_grants,
            "total_rejections": snap.total_rejections,
            "total_timeouts": snap.total_timeouts,
            "active_leases": snap.active_leases,
            "snapshots": snapshot_list,
            "blocked_would_be": self.governor.blocked_would_be_counts(),
            "capacity_recommendations": self.capacity_recommendations(),
        }

    def capacity_recommendations(self) -> dict[str, dict[str, int]]:
        """Derive observe-mode sizing facts from bounded real-load samples."""
        recommendations: dict[str, dict[str, int]] = {}
        for kind in ResourceKind:
            observed = [
                snap.snapshots[kind].reserved
                for snap in self.snapshot_history
                if kind in snap.snapshots
            ]
            queued = [
                snap.snapshots[kind].queued
                for snap in self.snapshot_history
                if kind in snap.snapshots
            ]
            if not observed:
                continue
            peak = max(observed)
            peak_queue = max(queued, default=0)
            recommendations[kind.value] = {
                "observed_peak": peak,
                "observed_peak_queue": peak_queue,
                # Leave one slot of burst headroom only when contention was
                # actually observed; never recommend below observed demand.
                "suggested_limit": peak + (1 if peak_queue else 0),
                "sample_count": len(observed),
            }
        return recommendations

    def pressure_summary(self) -> dict[str, Any]:
        """返回当前压力等级的简要摘要。

        适用于日志、health check 和快速监控面板。
        """
        snap = self.latest()
        if snap is None:
            return {"overall": "unknown", "critical_resources": []}

        critical: list[str] = []
        warning: list[str] = []
        for kind, ks in snap.snapshots.items():
            if ks.pressure is ResourcePressure.CRITICAL:
                critical.append(kind.name)
            elif ks.pressure is ResourcePressure.WARNING:
                warning.append(kind.name)

        overall = ResourcePressure.NORMAL
        if critical:
            overall = ResourcePressure.CRITICAL
        elif warning:
            overall = ResourcePressure.WARNING

        return {
            "overall": overall.name,
            "critical_resources": critical,
            "warning_resources": warning,
            "active_leases": snap.active_leases,
        }
