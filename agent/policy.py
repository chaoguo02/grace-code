"""Task policy model and parser.

A TaskPolicy is the runtime contract for a task. It is derived from the
user request and then consumed by tool-policy and completion layers. Prompts can
show it to the model, but enforcement must happen outside the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.task import Task

READ_TOOLS = frozenset({"file_read", "file_view"})
WRITE_TOOLS = frozenset({"file_write", "file_edit"})
DISCOVERY_TOOLS = frozenset({"find_files", "find_symbol", "search_text"})
GIT_TOOLS = frozenset({"git_status", "git_diff", "git_add", "git_commit"})
WEB_TOOLS = frozenset({"web_search", "web_fetch"})
MEMORY_TOOLS = frozenset({"memory_read", "memory_write", "memory_list", "memory_delete", "memory_search"})
COMMAND_TOOLS = frozenset({"shell"})
TEST_TOOLS = frozenset({"pytest", "test"})
READONLY_TOOLS = READ_TOOLS | DISCOVERY_TOOLS | frozenset({"git_status", "git_diff"}) | WEB_TOOLS | frozenset({"memory_read", "memory_list"})

NO_OTHER_FILES_RE = re.compile(
    r"(不要|不得|禁止|别|do not|don't)\s*(?:查看|读取|修改|编辑|改动|read|inspect|view|open|modify|edit)[^\n。；;]*?(?:其他|其它|other)\s*(?:文件|files?)",
    re.IGNORECASE,
)
NO_SHELL_RE = re.compile(r"(不要|不得|禁止|别|do not|don't)\s*(?:运行|执行)?\s*(?:命令|shell|command)", re.IGNORECASE)
NO_TEST_RE = re.compile(r"(不要|不得|禁止|别|do not|don't)\s*(?:运行|执行|跑)?\s*(?:测试|test|pytest)", re.IGNORECASE)
NO_WEB_RE = re.compile(r"(不要|不得|禁止|别|do not|don't)\s*(?:联网|使用网络|web|搜索网页|web_search|web_fetch)", re.IGNORECASE)
NO_MEMORY_RE = re.compile(r"(不要|不得|禁止|别|do not|don't)\s*(?:使用)?\s*(?:记忆|memory)", re.IGNORECASE)


def normalize_repo_path(path_text: str, repo_path: str) -> str:
    normalized = path_text.strip().strip("`'\"，,。.;；:：")
    normalized = normalized.replace("\\", "/")
    if normalized.lower() == "readme":
        normalized = "README.md"
    candidate = Path(normalized)
    if candidate.is_absolute():
        try:
            normalized = candidate.resolve().relative_to(Path(repo_path).resolve()).as_posix()
        except ValueError:
            normalized = candidate.as_posix()
    return normalized.lstrip("./")


@dataclass(frozen=True)
class PhasePolicy:
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_read_paths: frozenset[str] | None = None
    allowed_write_paths: frozenset[str] | None = None
    strict_file_scope: bool = False
    notes: tuple[str, ...] = ()

    def with_allowed_tools(self, allowed_tools: set[str] | frozenset[str]) -> "PhasePolicy":
        allowed = frozenset(allowed_tools)
        if self.allowed_tools is not None:
            allowed = allowed & self.allowed_tools
        return PhasePolicy(
            allowed_tools=allowed,
            denied_tools=self.denied_tools,
            allowed_read_paths=self.allowed_read_paths,
            allowed_write_paths=self.allowed_write_paths,
            strict_file_scope=self.strict_file_scope,
            notes=self.notes,
        )

    def to_prompt_section(self, title: str = "Task Tool Constraints") -> str:
        lines: list[str] = []
        if self.allowed_tools is not None:
            lines.append(f"- Allowed tools: {', '.join(sorted(self.allowed_tools)) or '(none)'}")
        if self.denied_tools:
            lines.append(f"- Blocked tools: {', '.join(sorted(self.denied_tools))}")
        if self.strict_file_scope:
            lines.append("- Strict file scope is active; do not access files outside the allowed paths.")
        if self.allowed_read_paths is not None:
            lines.append(f"- Allowed read paths: {', '.join(sorted(self.allowed_read_paths)) or '(none)'}")
        if self.allowed_write_paths is not None:
            lines.append(f"- Allowed write paths: {', '.join(sorted(self.allowed_write_paths)) or '(none)'}")
        for note in self.notes:
            lines.append(f"- {note}")
        if not lines:
            return ""
        return f"## {title}\n" + "\n".join(lines)


@dataclass(frozen=True)
class CompletionPolicy:
    required_reads: frozenset[str] = field(default_factory=frozenset)
    required_writes: frozenset[str] = field(default_factory=frozenset)
    forbidden_tools: frozenset[str] = field(default_factory=frozenset)
    require_any_write: bool = False
    require_any_read: bool = False
    strict_file_scope: bool = False


@dataclass(frozen=True)
class TaskPolicy:
    intent: str
    planning: PhasePolicy
    execution: PhasePolicy
    completion: CompletionPolicy
    notes: tuple[str, ...] = ()

    @property
    def allowed_read_paths(self) -> frozenset[str] | None:
        return self.execution.allowed_read_paths

    @property
    def allowed_write_paths(self) -> frozenset[str] | None:
        return self.execution.allowed_write_paths

    @property
    def blocked_tools(self) -> frozenset[str]:
        return self.execution.denied_tools

    @property
    def strict_file_scope(self) -> bool:
        return self.execution.strict_file_scope

    @property
    def has_path_scope(self) -> bool:
        return bool(self.allowed_read_paths or self.allowed_write_paths)

    def to_prompt_section(self, phase: str | None = None) -> str:
        phase_policy = self.planning if phase == "planning" else self.execution
        lines: list[str] = []
        if phase:
            lines.append(f"- Current phase: {phase}.")
        if phase_policy.allowed_tools is not None:
            lines.append(f"- Allowed tools: {', '.join(sorted(phase_policy.allowed_tools)) or '(none)'}")
        if phase_policy.denied_tools:
            lines.append(f"- Blocked tools: {', '.join(sorted(phase_policy.denied_tools))}")
        if phase_policy.strict_file_scope:
            lines.append("- Strict file scope is active; do not access files outside the allowed paths.")
        if phase_policy.allowed_read_paths is not None:
            lines.append(f"- Allowed read paths: {', '.join(sorted(phase_policy.allowed_read_paths)) or '(none)'}")
        if phase_policy.allowed_write_paths is not None:
            lines.append(f"- Allowed write paths: {', '.join(sorted(phase_policy.allowed_write_paths)) or '(none)'}")
        if self.completion.required_reads:
            lines.append(f"- Completion requires reading: {', '.join(sorted(self.completion.required_reads))}")
        if self.completion.required_writes:
            lines.append(f"- Completion requires writing: {', '.join(sorted(self.completion.required_writes))}")
        if self.completion.require_any_write:
            lines.append("- Completion requires at least one file write tool call.")
        if self.completion.require_any_read:
            lines.append("- Completion requires at least one successful file read tool call.")
        for note in self.notes:
            lines.append(f"- {note}")
        if not lines:
            return ""
        return "## Task Policy\n" + "\n".join(lines)


def _blocked_tools_from_text(description: str) -> tuple[set[str], list[str]]:
    blocked_tools: set[str] = set()
    notes: list[str] = []
    if NO_SHELL_RE.search(description):
        blocked_tools.update(COMMAND_TOOLS)
        notes.append("Shell/command execution is disabled by the user request.")
    if NO_TEST_RE.search(description):
        blocked_tools.update(TEST_TOOLS)
        notes.append("Test execution is disabled by the user request.")
    if NO_WEB_RE.search(description):
        blocked_tools.update(WEB_TOOLS)
        notes.append("Web access is disabled by the user request.")
    if NO_MEMORY_RE.search(description):
        blocked_tools.update(MEMORY_TOOLS)
        notes.append("Memory tools are disabled by the user request.")
    return blocked_tools, notes


def build_task_policy(task: Task) -> TaskPolicy:
    description = task.description
    intent = task.intent

    explicit_read_paths = task.explicit_read_paths
    explicit_write_paths = task.explicit_write_paths
    strict_file_scope = bool(NO_OTHER_FILES_RE.search(description))

    blocked_tools, notes = _blocked_tools_from_text(description)
    if strict_file_scope:
        blocked_tools.update(WEB_TOOLS)
        blocked_tools.update(MEMORY_TOOLS)
        notes.append("Do not claim to have inspected files unless a tool call actually read them.")

    # 路径限定仅来自用户显式声明，不做 NLP 推断
    allowed_read_paths: frozenset[str] | None = explicit_read_paths
    allowed_write_paths: frozenset[str] | None = explicit_write_paths

    if intent == "edit" and explicit_write_paths:
        allowed_read_paths = frozenset(set(allowed_read_paths or ()) | explicit_write_paths)

    if intent == "analysis":
        planning_allowed = frozenset()
        execution_allowed = READ_TOOLS if allowed_read_paths else READONLY_TOOLS
        required_reads = frozenset(allowed_read_paths or ())
        required_writes = frozenset()
        require_any_write = False
        require_any_read = bool(strict_file_scope and not allowed_read_paths)
    else:
        planning_allowed = READONLY_TOOLS
        execution_allowed = None
        required_reads = frozenset()
        required_writes = frozenset(allowed_write_paths or ())
        require_any_write = not bool(required_writes)
        require_any_read = False

    planning = PhasePolicy(
        allowed_tools=planning_allowed,
        denied_tools=frozenset(blocked_tools),
        allowed_read_paths=allowed_read_paths,
        allowed_write_paths=None,
        strict_file_scope=strict_file_scope,
        notes=tuple(notes),
    )
    execution = PhasePolicy(
        allowed_tools=execution_allowed,
        denied_tools=frozenset(blocked_tools),
        allowed_read_paths=allowed_read_paths,
        allowed_write_paths=allowed_write_paths,
        strict_file_scope=strict_file_scope,
        notes=tuple(notes),
    )
    completion = CompletionPolicy(
        required_reads=required_reads,
        required_writes=required_writes,
        forbidden_tools=frozenset(blocked_tools),
        require_any_write=require_any_write,
        require_any_read=require_any_read,
        strict_file_scope=strict_file_scope,
    )
    return TaskPolicy(
        intent=intent,
        planning=planning,
        execution=execution,
        completion=completion,
        notes=tuple(notes),
    )
