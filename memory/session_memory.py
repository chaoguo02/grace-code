"""
memory/session_memory.py

SessionMemory — session-level auto-extraction aligned with Claude Code's
sessionMemory.ts behavior.

Trigger thresholds:
- Initial extraction at 10K cumulative tokens
- Subsequent extraction after 5K token growth OR 3 tool calls

Extraction is non-blocking and delegated to a restricted session-memory
subagent runner. The default runner uses a background thread and is restricted
to writing only the configured session notes file.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from llm.base import LLMBackend

logger = logging.getLogger(__name__)

INIT_TOKEN_THRESHOLD = 10_000
UPDATE_TOKEN_DELTA = 5_000
UPDATE_TOOL_CALLS = 3
MAX_SESSION_NOTES_TOKENS = 12_000
SECTION_TOKEN_BUDGET = 2_000

SESSION_NOTES_TEMPLATE = """\
# {title}
（5-10个词的简短标题，信息密集，不废话）

# 当前状态
现在正在做什么？还没完成的任务。下一步马上要做的事。

# 任务说明
用户让做什么？有哪些设计要求、背景信息。

# 文件与功能
重要文件有哪些？简单说它们是干嘛的、为什么重要。

# 工作流程
通常要运行哪些命令？按什么顺序？命令结果怎么看懂。

# 错误与修正
遇到了什么错误？怎么修好的？用户纠正过什么？哪些方法失败了、不要再试。

# 系统文档
重要的系统组件有哪些？它们怎么配合工作。

# 经验总结
什么有效？什么无效？要避免什么？不重复其他部分内容。

# 关键结果
如果用户要了明确结果（答案、表格、文档），在这里完整写出来。

# 工作记录
一步一步做了什么？每一步极简总结。
"""

EXTRACTION_SYSTEM_PROMPT = f"""\
You are a session-memory subagent for a coding agent. Update exactly one
session notes file from the current conversation context.

Tool and path restrictions:
- You may only write the provided session notes file path.
- You may not read or write any other path.
- Preserve the exact 10-section template and all section headings.

Strict rules:
- Never modify, delete, or add section headings (lines starting with #).
- Never modify or delete the explanatory line under each section heading.
- Write detailed, useful content with file paths, function names, commands,
  errors, fixes, and user corrections when available.
- Keep each section to approximately {SECTION_TOKEN_BUDGET} tokens.
- Keep the whole notes file under approximately {MAX_SESSION_NOTES_TOKENS} tokens.
- Always update 当前状态 to reflect the latest work.

Return the COMPLETE updated notes file content.
"""


class SessionMemorySubagentRunner(Protocol):
    """Restricted non-blocking runner for session-memory extraction."""

    allowed_tools: tuple[str, ...]
    allowed_paths: tuple[Path, ...]

    def fork(self, *, prompt: str, notes_path: Path, current_notes: str) -> None:
        """Start extraction in the background."""


@dataclass(frozen=True)
class SessionMemoryExtractionRequest:
    prompt: str
    notes_path: Path
    current_notes: str


class ThreadedSessionMemorySubagent:
    """
    Default restricted session-memory subagent implementation.

    It runs a single LLM call in a daemon thread and writes only notes_path.
    The public allowed_tools/allowed_paths fields make the restriction explicit
    and testable even though this implementation is local rather than a full
    child-session runtime.
    """

    allowed_tools: tuple[str, ...] = ("file_write",)

    def __init__(self, backend: "LLMBackend", notes_path: Path) -> None:
        from tools.file_tool import FileWriteTool

        self._backend = backend
        self.allowed_paths = (notes_path.resolve(),)
        self._write_tool = FileWriteTool(allowed_paths=list(self.allowed_paths))
        self._running = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._running

    def fork(self, *, prompt: str, notes_path: Path, current_notes: str) -> None:
        resolved_notes_path = notes_path.resolve()
        if resolved_notes_path not in self.allowed_paths:
            raise ValueError(f"session-memory runner cannot write outside allowed path: {notes_path}")

        with self._lock:
            if self._running:
                return
            self._running = True

        thread = threading.Thread(
            target=self._run,
            args=(SessionMemoryExtractionRequest(prompt, resolved_notes_path, current_notes),),
            daemon=True,
            name="session-memory-subagent",
        )
        thread.start()

    def _run(self, request: SessionMemoryExtractionRequest) -> None:
        from llm.base import LLMMessage

        try:
            messages = [
                LLMMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
                LLMMessage(role="user", content=request.prompt),
            ]
            response = self._backend.complete(messages, tools=[])
            updated = _response_text(response).strip()
            if _is_valid_notes_output(updated):
                result = self._write_tool.execute({"path": str(request.notes_path), "content": updated})
                if result.success:
                    logger.info("Session memory updated: %s", request.notes_path.name)
                else:
                    logger.debug("Session memory write denied or failed: %s", result.error)
            else:
                logger.debug("Session memory extraction produced invalid output, skipping")
        except Exception as exc:
            logger.debug("Session memory extraction failed: %s", exc)
        finally:
            with self._lock:
                self._running = False


class SessionMemoryTracker:
    """Tracks SessionMemory thresholds and forks restricted extraction."""

    def __init__(
        self,
        *,
        backend: "LLMBackend",
        notes_path: Path,
        session_title: str = "Untitled Session",
        runner: SessionMemorySubagentRunner | None = None,
    ) -> None:
        self._notes_path = notes_path
        self._session_title = session_title
        self._runner = runner or ThreadedSessionMemorySubagent(backend, notes_path)
        self._last_extracted_tokens = 0
        self._last_tool_call_count = 0

    @property
    def notes_path(self) -> Path:
        return self._notes_path

    @property
    def runner(self) -> SessionMemorySubagentRunner:
        return self._runner

    def tick(
        self,
        current_tokens: int,
        current_tool_calls: int,
        context_summary: str = "",
        last_turn_had_tools: bool | None = None,
    ) -> bool:
        """Check thresholds and fork extraction when needed."""
        del last_turn_had_tools  # Compatibility with older callers; not a trigger.

        if getattr(self._runner, "running", False):
            return False

        if self._last_extracted_tokens == 0:
            if current_tokens < INIT_TOKEN_THRESHOLD:
                return False
        else:
            token_growth = current_tokens - self._last_extracted_tokens
            tool_growth = current_tool_calls - self._last_tool_call_count
            if token_growth < UPDATE_TOKEN_DELTA and tool_growth < UPDATE_TOOL_CALLS:
                return False

        if not context_summary.strip():
            return False

        current_notes = self._read_or_create_notes()
        prompt = self._build_prompt(context_summary, current_notes)
        self._last_extracted_tokens = current_tokens
        self._last_tool_call_count = current_tool_calls
        self._runner.fork(prompt=prompt, notes_path=self._notes_path, current_notes=current_notes)
        return True

    def finalize(self) -> None:
        """Ensure the session notes file exists at session end."""
        self._read_or_create_notes()

    def _read_or_create_notes(self) -> str:
        if self._notes_path.exists():
            return self._notes_path.read_text(encoding="utf-8")
        from tools.file_tool import FileWriteTool

        notes = SESSION_NOTES_TEMPLATE.format(title=self._session_title)
        write_tool = FileWriteTool(allowed_paths=[self._notes_path.resolve()])
        result = write_tool.execute({"path": str(self._notes_path), "content": notes})
        if not result.success:
            raise PermissionError(result.error or f"Unable to create session notes: {self._notes_path}")
        return notes

    def _build_prompt(self, context_summary: str, current_notes: str) -> str:
        return (
            f"Session notes path (the only allowed write target): {self._notes_path}\n\n"
            f"Current conversation context:\n{context_summary}\n\n"
            f"<current_notes_content>\n{current_notes}\n</current_notes_content>\n\n"
            "Update the notes to reflect the latest work. Return the COMPLETE updated file content."
        )


def _response_text(response: object) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    action = getattr(response, "action", None)
    if action is not None:
        message = getattr(action, "message", None)
        if isinstance(message, str) and message:
            return message
        thought = getattr(action, "thought", None)
        if isinstance(thought, str):
            return thought
    raw_content = getattr(response, "raw_content", None)
    return raw_content if isinstance(raw_content, str) else ""


def _is_valid_notes_output(text: str) -> bool:
    if not text:
        return False
    required_headings = [
        "# 当前状态",
        "# 任务说明",
        "# 文件与功能",
        "# 工作流程",
        "# 错误与修正",
        "# 系统文档",
        "# 经验总结",
        "# 关键结果",
        "# 工作记录",
    ]
    return all(heading in text for heading in required_headings)
