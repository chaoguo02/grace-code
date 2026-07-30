"""
memory/dream_agent.py

Restricted DreamAgent for LLM-driven memory consolidation.

Architecture-aligned with public Claude Code analyses:
- forked/background-style executor interface
- max 5 turns, matching confirmed extractMemories.ts fork-agent safety bound
- read/grep/write tool surface
- write_file is hard-restricted to memory_dir
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm.base import LLMMessage
from llm.provider_capacity import complete_with_provider_capacity
from memory.consolidation_prompt import CONSOLIDATION_PROMPT
from memory.store import _atomic_write_text, _truncate_index

MAX_DREAM_TURNS = 5
@dataclass
class DreamAgentResult:
    files_created: list[str] = field(default_factory=list)
    files_updated: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    contradictions_resolved: list[str] = field(default_factory=list)
    summary: str = ""
    aborted: bool = False
    turns_used: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.files_created or self.files_updated or self.files_deleted or self.contradictions_resolved)


class DreamAgent:
    """
    Restricted memory consolidation agent.

    The backend is expected to return either:
    - JSON with {"tool_calls": [{"name": ..., "arguments": {...}}], "summary": "..."}
    - or plain text summary with no tool calls.
    """

    def __init__(
        self, memory_dir: Path, backend: Any, *, workspace_root: Path | None = None,
    ) -> None:
        self.memory_dir = memory_dir.resolve()
        self.workspace_root = (workspace_root or memory_dir).resolve()
        self.backend = backend
        self._abort = threading.Event()
        self._thread: threading.Thread | None = None
        self._async_result: DreamAgentResult | None = None

    def run_async(self) -> threading.Thread:
        """Start DreamAgent in a daemon thread and return immediately."""
        self._abort.clear()
        self._thread = threading.Thread(
            target=self._run_async_target,
            daemon=True,
            name="dream-consolidation",
        )
        self._thread.start()
        return self._thread

    def abort(self) -> None:
        """Request background DreamAgent cancellation."""
        self._abort.set()

    @property
    def async_result(self) -> DreamAgentResult | None:
        return self._async_result

    def _run_async_target(self) -> None:
        try:
            self._async_result = self.run()
        except Exception as exc:
            self._async_result = DreamAgentResult(summary=f"DreamAgent failed: {exc}", aborted=self._abort.is_set())

    def run(self) -> DreamAgentResult:
        messages = self._build_messages()
        aggregate = DreamAgentResult()
        for turn in range(1, MAX_DREAM_TURNS + 1):
            if self._abort.is_set():
                aggregate.aborted = True
                break
            response = complete_with_provider_capacity(
                self.backend,
                messages,
                self._tool_schemas(),
            )
            from llm.response_utils import response_text

            raw = response_text(response)
            turn_result, tool_output = self._execute_response(raw)
            aggregate.files_created.extend(turn_result.files_created)
            aggregate.files_updated.extend(turn_result.files_updated)
            aggregate.files_deleted.extend(turn_result.files_deleted)
            aggregate.contradictions_resolved.extend(turn_result.contradictions_resolved)
            aggregate.summary = turn_result.summary or aggregate.summary
            aggregate.turns_used = turn
            if not tool_output:
                break
            messages.append(LLMMessage(role="assistant", content=raw))
            messages.append(LLMMessage(role="user", content=f"Tool results:\n{json.dumps(tool_output, ensure_ascii=False)}"))
        return aggregate

    def _build_messages(self) -> list[LLMMessage]:
        context_parts: list[str] = []
        if self.memory_dir.exists():
            files = sorted(self.memory_dir.rglob("*.md"))
            file_lines = []
            for path in files:
                freshness = self._memory_freshness_text(path)
                file_lines.append(f"- {path.relative_to(self.memory_dir)}{freshness}")
            context_parts.append(f"Memory directory contents:\n{chr(10).join(file_lines) or '(empty)'}")

        memory_index = self.memory_dir / "MEMORY.md"
        if memory_index.exists():
            context_parts.append(f"MEMORY.md:\n{memory_index.read_text(encoding='utf-8')}")

        return [
            LLMMessage(role="system", content=CONSOLIDATION_PROMPT),
            LLMMessage(role="user", content="\n\n".join(context_parts) or "Memory directory is empty."),
        ]

    def _tool_schemas(self) -> list[dict[str, Any]]:
        """Declarative tool schemas for the restricted memory consolidation agent.

        The agent can READ from any path (memory_dir + workspace_root).
        write_file is hard-restricted to write only within memory_dir.
        CC-aligned: consolidation agent can access project workspace to verify
        whether memories are still relevant.
        """
        return [
            {
                "name": "read_file",
                "description": "Read a file (read-only)",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "grep",
                "description": "Search for patterns in files (read-only)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["pattern", "path"],
                },
            },
            {
                "name": "write_file",
                "description": f"Write a file. ONLY allowed within: {self.memory_dir}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        ]

    # Declarative tool dispatch: tool_name → (handler_method, output_limit, writes_files)
    _TOOL_DISPATCH: dict[str, tuple[str, int | None, bool]] = {
        "read_file": ("_read_file", 4000, False),
        "grep": ("_grep", 100, False),
        "write_file": ("_write_file", None, True),
    }

    def _execute_response(self, raw: str) -> tuple[DreamAgentResult, list[dict[str, Any]]]:
        result = DreamAgentResult(summary=raw.strip())
        tool_output: list[dict[str, Any]] = []
        payload = self._parse_payload(raw)
        if not isinstance(payload, dict):
            return result, tool_output

        result.summary = str(payload.get("summary") or result.summary)
        for call in payload.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            args = call.get("arguments") or {}
            dispatch = self._TOOL_DISPATCH.get(name or "")
            if dispatch is None:
                tool_output.append({"name": name, "output": f"Unknown tool: {name}"})
                continue
            handler_name, output_limit, writes_files = dispatch
            handler = getattr(self, handler_name)
            output = handler(args)
            if writes_files:
                written_path, created = output
                if created:
                    result.files_created.append(str(written_path))
                else:
                    result.files_updated.append(str(written_path))
                tool_output.append({"name": name, "output": str(written_path)})
            else:
                tool_output.append({
                    "name": name,
                    "output": output[:output_limit] if output_limit else str(output),
                })
        return result, tool_output

    def _read_file(self, args: dict[str, Any]) -> str:
        path = self._resolve_read_path(args.get("path"))
        return path.read_text(encoding="utf-8", errors="replace")

    def _grep(self, args: dict[str, Any]) -> list[str]:
        pattern = str(args.get("pattern") or "")
        root = self._resolve_read_path(args.get("path"))
        regex = re.compile(pattern)
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if regex.search(line):
                        matches.append(f"{path}: {line}")
            except OSError:
                continue
        return matches

    def _write_file(self, args: dict[str, Any]) -> tuple[Path, bool]:
        target = self._resolve_write_path(args.get("path"))
        content = str(args.get("content") or "")
        if target.name == "MEMORY.md":
            content = _truncate_index(content)
        created = not target.exists()
        _atomic_write_text(target, content)
        return target, created

    def _resolve_read_path(self, raw_path: Any) -> Path:
        """Resolve a read path relative to memory_dir or workspace_root.

        CC-aligned (M7): consolidation agent can read the full workspace
        to verify whether memories are still relevant. Write is still
        restricted to memory_dir only.
        """
        path = Path(str(raw_path or ""))
        if not path.is_absolute():
            # Try workspace_root first (preferred for project context)
            _tentative = (self.workspace_root / path).resolve()
            if _tentative.exists():
                return _tentative
            path = self.memory_dir / path
        resolved = path.resolve()
        # Ensure the resolved path is within memory_dir or workspace_root
        _allowed = (self.memory_dir, self.workspace_root)
        if not any(self._is_within(base, resolved) for base in _allowed):
            raise PermissionError(
                f"Read blocked: {resolved} is outside allowed directories"
            )
        return resolved

    @staticmethod
    def _is_within(base: Path, target: Path) -> bool:
        """Check if target path is within base directory."""
        try:
            target.relative_to(base)
            return True
        except ValueError:
            return False

    def _resolve_write_path(self, raw_path: Any) -> Path:
        path = self._resolve_read_path(raw_path)
        try:
            path.relative_to(self.memory_dir)
        except ValueError as exc:
            raise PermissionError(f"Write blocked: {path} is outside memory directory") from exc
        return path

    def _memory_freshness_text(self, file_path: Path) -> str:
        try:
            age_days = int((time.time() - file_path.stat().st_mtime) / 86400)
        except OSError:
            return ""
        if age_days <= 1:
            return ""
        return (
            f" [This memory is {age_days} days old. Memories are point-in-time "
            "observations, not live state — verify before acting on this information.]"
        )

    @staticmethod
    def _parse_payload(raw: str) -> Any:
        """Parse the LLM response as JSON.  When using a backend that supports
        native tool_use, this path is only hit for the summary (not tool calls).

        Claude Code pattern: native tool_use blocks exclusively, zero regex.
        """
        import json
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
