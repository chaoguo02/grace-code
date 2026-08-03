"""
G34: Evidence Collector — immutable evidence from tool outcomes + workspace facts.

- Runtime produces ToolEvidence during execution.
- Collector aggregates evidence; persistence is Coordinator's job in terminal UoW.
- WorkspaceFacts include file modifications, created files, deleted files.
- snapshot() produces immutable RunEvidence value object.
- No DB writes; no background workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime_core.outcome import ToolEvidence, RunEvidence


@dataclass(frozen=True, slots=True)
class WorkspaceFact:
    """Immutable record of a workspace change."""
    path: str
    action: str = ""  # created | modified | deleted
    bytes_before: int = 0
    bytes_after: int = 0


class EvidenceCollector:
    """Collects immutable evidence during a single run execution.

    G34: Runtime records evidence; Coordinator persists it in terminal UoW.
         No DB access, no background workers, no daemon threads.
    """

    def __init__(self) -> None:
        self._tools: list[ToolEvidence] = []
        self._workspace_facts: list[WorkspaceFact] = []
        self._files_touched: set[str] = set()
        self._hook_blocks: list[str] = []
        self._errors: list[str] = []

    # ── Recording ──────────────────────────────────────────────────────

    def record_tool(self, tool_name: str, success: bool,
                    duration_ms: float = 0.0) -> None:
        """Record a tool execution.  Multiple calls per run are expected."""
        self._tools.append(ToolEvidence(
            tool_name=tool_name, success=success, duration_ms=duration_ms,
        ))

    def record_workspace_fact(self, path: str, action: str,
                              bytes_before: int = 0,
                              bytes_after: int = 0) -> None:
        """Record a workspace change (file created/modified/deleted)."""
        self._workspace_facts.append(WorkspaceFact(
            path=path, action=action,
            bytes_before=bytes_before, bytes_after=bytes_after,
        ))
        self._files_touched.add(path)

    def record_file_touched(self, path: str) -> None:
        """Record a file that was accessed (read or written)."""
        self._files_touched.add(path)

    def record_hook_block(self, hook_name: str) -> None:
        """Record a hook that blocked an operation."""
        self._hook_blocks.append(hook_name)

    def record_error(self, error: str) -> None:
        """Record a non-fatal error encountered during the run."""
        self._errors.append(error)

    # ── Snapshot ───────────────────────────────────────────────────────

    def snapshot(self) -> RunEvidence:
        """Produce an immutable evidence snapshot.

        G34: The returned RunEvidence is a value object — no repository rows.
             Coordinator persists it in the terminal UoW transaction.
             Each snapshot() call returns a new independent object.
        """
        return RunEvidence(
            tool_calls=tuple(self._tools),
            files_touched=tuple(sorted(self._files_touched)),
            hook_blocks=tuple(self._hook_blocks),
        )

    def workspace_snapshot(self) -> tuple[WorkspaceFact, ...]:
        """Immutable snapshot of workspace facts only."""
        return tuple(self._workspace_facts)

    # ── Query ──────────────────────────────────────────────────────────

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    @property
    def files_touched_count(self) -> int:
        return len(self._files_touched)

    @property
    def error_count(self) -> int:
        return len(self._errors)

    @property
    def has_errors(self) -> bool:
        return len(self._errors) > 0
