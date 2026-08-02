"""
context/stats.py

上下文观测数据结构。

ContextStats 记录单次 LLM request 的上下文各层 token 占用。
ContextTrace 记录该 request 的上下文决策日志（包含/省略/压缩/artifact/检索）。

设计原则：
- 纯数据结构，不改变运行时行为
- 由 agent/core.py 或未来 ContextManager 在组装 prompt 时填充
- /stats 和 verbose 日志从这里读取
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextStats:
    """单次 LLM request 的上下文 token 占用分解。"""

    # 预算上限
    request_budget_tokens: int = 0

    # 各层实际估算 token 数
    system_tokens: int = 0
    project_tokens: int = 0        # repo_map + project rules + skills
    memory_tokens: int = 0         # long-term memory injection
    session_tokens: int = 0        # rolling session summary / completed task summaries
    task_tokens: int = 0           # current task working messages
    repo_map_tokens: int = 0       # repo map 单独拆出（project 的子集）
    artifact_summary_tokens: int = 0  # artifact 摘要

    # broad analysis phase metadata
    analysis_phase: str = ""
    analysis_files_read: int = 0
    analysis_inspect_reads: int = 0
    analysis_verify_reads: int = 0
    analysis_evidence_records: int = 0
    analysis_phase_summaries: int = 0
    analysis_claims: int = 0
    analysis_tool_decisions: int = 0
    analysis_recovery_actions: int = 0
    analysis_deferred_reads: int = 0
    analysis_phase_token_costs: dict[str, int] = field(default_factory=dict)

    # 聚合
    estimated_total_tokens: int = 0
    omitted_tokens: int = 0        # 被裁剪/省略的内容估算 token

    # compaction 元数据
    compact_triggered: bool = False
    compact_reason: str = ""
    compact_method: str = ""
    compact_truncated: bool = False
    compact_source_range: tuple[int, int] | None = None

    @property
    def utilization(self) -> float:
        """context 利用率 (0.0~1.0+)。"""
        if self.request_budget_tokens <= 0:
            return 0.0
        return self.estimated_total_tokens / self.request_budget_tokens

    def summary_line(self) -> str:
        """一行摘要，适合在 /stats 或 verbose 输出中展示。"""
        parts = [
            f"total {_k(self.estimated_total_tokens)}/{_k(self.request_budget_tokens)}",
            f"system {_k(self.system_tokens)}",
            f"repo {_k(self.repo_map_tokens)}",
            f"memory {_k(self.memory_tokens)}",
            f"session {_k(self.session_tokens)}",
            f"task {_k(self.task_tokens)}",
        ]
        if self.artifact_summary_tokens:
            parts.append(f"artifacts {_k(self.artifact_summary_tokens)}")
        if self.analysis_phase:
            parts.append(
                f"analysis {self.analysis_phase} "
                f"files {self.analysis_files_read} "
                f"verify {self.analysis_verify_reads} "
                f"evidence {self.analysis_evidence_records} "
                f"claims {self.analysis_claims} "
                f"decisions {self.analysis_tool_decisions} "
                f"recoveries {self.analysis_recovery_actions} "
                f"deferred {self.analysis_deferred_reads} "
                f"summaries {self.analysis_phase_summaries}"
            )
            if self.analysis_phase_token_costs:
                parts.append(f"phase_costs {_phase_costs(self.analysis_phase_token_costs)}")
        if self.omitted_tokens:
            parts.append(f"omitted {_k(self.omitted_tokens)}")
        parts.append(f"compact {'yes' if self.compact_triggered else 'no'}")
        return "Context: " + " · ".join(parts)


@dataclass
class ContextTrace:
    """单次 LLM request 的上下文决策完整日志。"""

    task_id: str = ""
    step: int = 0
    stats: ContextStats = field(default_factory=ContextStats)

    # 决策明细
    included: list[str] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    compactions: list[str] = field(default_factory=list)
    artifacts_created: list[str] = field(default_factory=list)
    retrievals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """序列化为 dict，方便写入 EventLog 或 JSON。"""
        return {
            "task_id": self.task_id,
            "step": self.step,
            "stats": {
                "request_budget_tokens": self.stats.request_budget_tokens,
                "estimated_total_tokens": self.stats.estimated_total_tokens,
                "system_tokens": self.stats.system_tokens,
                "project_tokens": self.stats.project_tokens,
                "memory_tokens": self.stats.memory_tokens,
                "session_tokens": self.stats.session_tokens,
                "task_tokens": self.stats.task_tokens,
                "repo_map_tokens": self.stats.repo_map_tokens,
                "artifact_summary_tokens": self.stats.artifact_summary_tokens,
                "analysis_phase": self.stats.analysis_phase,
                "analysis_files_read": self.stats.analysis_files_read,
                "analysis_inspect_reads": self.stats.analysis_inspect_reads,
                "analysis_verify_reads": self.stats.analysis_verify_reads,
                "analysis_evidence_records": self.stats.analysis_evidence_records,
                "analysis_phase_summaries": self.stats.analysis_phase_summaries,
                "analysis_claims": self.stats.analysis_claims,
                "analysis_tool_decisions": self.stats.analysis_tool_decisions,
                "analysis_recovery_actions": self.stats.analysis_recovery_actions,
                "analysis_deferred_reads": self.stats.analysis_deferred_reads,
                "analysis_phase_token_costs": dict(self.stats.analysis_phase_token_costs),
                "omitted_tokens": self.stats.omitted_tokens,
                "compact_triggered": self.stats.compact_triggered,
                "compact_reason": self.stats.compact_reason,
                "compact_method": self.stats.compact_method,
                "compact_truncated": self.stats.compact_truncated,
                "compact_source_range": self.stats.compact_source_range,
            },
            "included": self.included,
            "omitted": self.omitted,
            "compactions": self.compactions,
            "artifacts_created": self.artifacts_created,
            "retrievals": self.retrievals,
        }


def _k(tokens: int) -> str:
    """将 token 数格式化为 'Nk' 形式。"""
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def _phase_costs(costs: dict[str, int]) -> str:
    parts = [f"{phase}={_k(tokens)}" for phase, tokens in costs.items() if tokens > 0]
    return ",".join(parts) if parts else "(none)"
