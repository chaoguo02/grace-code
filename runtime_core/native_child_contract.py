"""runtime_core/native_child_contract.py

Phase 1: Native Child Execution Contract — CC-aligned data types.

Fields map 1:1 to Claude Code Agent Tool schema:
  Input:  description, prompt, subagent_type, model?, run_in_background?, isolation?
  Output: status, agentId, content, totalToolUseCount, totalDurationMs, totalTokens

Grace Code extensions (error, structured_report, evidence_refs, worktree_disposition)
are clearly separated and do not affect CC-aligned core fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


# ── NativeChildRequest ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NativeChildRequest:
    """CC Agent Tool input — one child execution request.

    CC fields (1:1):
      description     — 3-5 word task summary
      prompt          — full task instructions
      subagent_type   — agent definition name (e.g. "explore", "general")
      model           — optional model override ("" = inherit parent)
      run_in_background — async launch (Phase 5)
      isolation       — "worktree" or "" (Phase 6)

    Grace Code extension:
      idempotency_key — dedup key (not in CC)
    """

    description: str
    prompt: str
    subagent_type: str
    model: str = ""
    run_in_background: bool = False
    isolation: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        _require_text(self.description, "description")
        _require_text(self.prompt, "prompt")
        _require_text(self.subagent_type, "subagent_type")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "description": self.description,
            "prompt": self.prompt,
            "subagent_type": self.subagent_type,
            "model": self.model,
            "run_in_background": self.run_in_background,
            "isolation": self.isolation,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NativeChildRequest":
        return cls(
            description=str(data.get("description", "")),
            prompt=str(data.get("prompt", "")),
            subagent_type=str(data.get("subagent_type", "")),
            model=str(data.get("model", "")),
            run_in_background=bool(data.get("run_in_background", False)),
            isolation=str(data.get("isolation", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
        )


# ── NativeChildResult ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NativeChildResult:
    """CC Agent Tool output — one child execution result.

    CC fields (1:1):
      status              — "completed" | "failed" | "cancelled"
      agent_id            — child session/run identifier (CC: agentId)
      content             — subagent final message (CC: content)
      total_tool_use_count — number of tool calls made (CC: totalToolUseCount)
      total_duration_ms    — wall-clock duration (CC: totalDurationMs)
      total_tokens         — tokens consumed (CC: totalTokens)

    Grace Code extensions:
      error               — error message when status != "completed"
      structured_report   — ReportFindings structured output (Phase 3+)
      evidence_refs       — references to persisted evidence
      worktree_disposition — "not_applicable" | "preserved" | "discarded"
    """

    status: str
    agent_id: str
    content: str
    total_tool_use_count: int = 0
    total_duration_ms: float = 0.0
    total_tokens: int = 0
    # ── Grace Code extensions ──
    error: str = ""
    structured_report: dict[str, JsonValue] | None = None
    evidence_refs: tuple[str, ...] = ()
    worktree_disposition: str = "not_applicable"

    def __post_init__(self) -> None:
        _require_text(self.status, "status")
        _require_text(self.agent_id, "agent_id")
        if self.total_tool_use_count < 0:
            raise ValueError("total_tool_use_count must be non-negative")
        if self.total_duration_ms < 0.0:
            raise ValueError("total_duration_ms must be non-negative")
        if self.total_tokens < 0:
            raise ValueError("total_tokens must be non-negative")
        if self.structured_report is not None and not isinstance(self.structured_report, Mapping):
            raise TypeError("structured_report must be a mapping")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "status": self.status,
            "agent_id": self.agent_id,
            "content": self.content,
            "total_tool_use_count": self.total_tool_use_count,
            "total_duration_ms": self.total_duration_ms,
            "total_tokens": self.total_tokens,
            "error": self.error,
            "structured_report": self.structured_report,
            "evidence_refs": list(self.evidence_refs),
            "worktree_disposition": self.worktree_disposition,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NativeChildResult":
        raw_report = data.get("structured_report")
        return cls(
            status=str(data.get("status", "")),
            agent_id=str(data.get("agent_id", "")),
            content=str(data.get("content", "")),
            total_tool_use_count=int(data.get("total_tool_use_count", 0)),
            total_duration_ms=float(data.get("total_duration_ms", 0.0)),
            total_tokens=int(data.get("total_tokens", 0)),
            error=str(data.get("error", "")),
            structured_report=(
                dict(raw_report) if raw_report is not None else None
            ),
            evidence_refs=tuple(_strings(data.get("evidence_refs", ()))),
            worktree_disposition=str(data.get("worktree_disposition", "not_applicable")),
        )


# ── helpers ───────────────────────────────────────────────────────────────────


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError("expected a list or tuple of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError("expected string values")
        result.append(item)
    return result
