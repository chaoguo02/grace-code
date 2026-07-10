"""
memory/proactive.py

主动记忆保存：通过规则检测模式自动触发 memory_write，不依赖 LLM 决策。

检测模式：
- 用户修正/反馈（"不要", "别再", "don't", "stop", "instead"）
- 构建/测试命令成功执行
- 用户确认偏好（"用这个", "always", "prefer"）
"""

from __future__ import annotations

import re
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 检测模式
# ---------------------------------------------------------------------------

# 用户修正模式（中英文）
_CORRECTION_PATTERNS = [
    re.compile(r"(?:不要|请不要|别再)(?:这样|那样|再|继续|总是)"),
    re.compile(r"不[是对].*[应该而]", re.UNICODE),
    re.compile(r"(?:以后|今后|之后|下次)(?:都|请|要|可以)"),
    re.compile(r"记住(?:这个|这一点|了)"),
    re.compile(r"\bdon'?t\b\s+(?:do|say|use|write|run|call|edit|change|modify|add|remove|create|delete|read|open|try|forget|repeat)"),
    re.compile(r"\bstop\b\s+\w+ing\b", re.IGNORECASE),
    re.compile(r"\bnever\b\s+(?:do|say|use|write|run|call|edit|change|modify|add|remove|create|delete)"),
    re.compile(r"\balways\b\s+(?:do|say|use|write|run|call|edit)"),
    re.compile(r"\binstead\b.{5,}", re.IGNORECASE),
    re.compile(r"\bprefer\b.{5,}", re.IGNORECASE),
    re.compile(r"\bremember\b\s+(?:that|this|to|the)", re.IGNORECASE),
]

# 构建/测试命令模式（从 shell 工具输出中检测）
_BUILD_CMD_PATTERNS = [
    re.compile(r"(?:npm|yarn|pnpm)\s+(?:run\s+)?(?:build|test|lint|dev|start)"),
    re.compile(r"(?:make|cmake)\s+\w+"),
    re.compile(r"(?:cargo)\s+(?:build|test|run|check)"),
    re.compile(r"(?:go)\s+(?:build|test|run)"),
    re.compile(r"(?:pytest|python\s+-m\s+pytest)"),
    re.compile(r"(?:pip\s+install)"),
    re.compile(r"(?:gradle|mvn)\s+\w+"),
]

# 成功输出标志
_SUCCESS_INDICATORS = [
    "passed", "success", "build complete", "compiled successfully",
    "all tests passed", "0 errors", "0 failed",
]


# ---------------------------------------------------------------------------
# ProactiveMemory
# ---------------------------------------------------------------------------

class ProactiveMemory:
    """
    主动记忆保存器。

    监听用户消息和工具输出，检测值得记住的模式，自动写入长期记忆。
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store
        self._saved_corrections: set[str] = set()
        self._saved_commands: set[str] = set()
        from memory.write_discipline import WriteDiscipline
        self._write_discipline = WriteDiscipline()

    def notify_explicit_memory_write(self) -> None:
        """
        Called when the user explicitly uses memory_write tool.

        Suppresses automatic extraction for this turn to avoid overwriting
        the user's deliberate intent (aligned with Claude Code's
        hasMemoryWritesSince() check).
        """
        self._write_discipline.notify_explicit_memory_write()

    def reset_turn(self) -> None:
        """Reset per-turn state. Call at the start of each new user message."""
        self._write_discipline.reset_turn()

    def check_user_message(self, user_input: str) -> None:
        """
        检查用户消息是否包含修正/偏好模式。
        匹配时自动保存为 feedback 类型记忆。

        Skipped if user has explicitly written memory this turn.
        """
        if self._write_discipline.should_skip_auto_extract():
            return

        if len(user_input) < 5 or len(user_input) > 500:
            return

        for pattern in _CORRECTION_PATTERNS:
            match = pattern.search(user_input)
            if match:
                matched_text = match.group(0).strip()
                # 去重：相同修正不重复保存
                dedup_key = matched_text[:50].lower()
                if dedup_key in self._saved_corrections:
                    return
                self._saved_corrections.add(dedup_key)

                self._save_feedback(user_input, matched_text)
                return

    def check_plan_feedback(self, feedback: str) -> None:
        """
        Capture plan rejection/revision feedback as procedural memory.

        Plan feedback always represents a deliberate design preference
        (unlike general chat which needs pattern matching to detect corrections).
        Only saves feedback with actionable content (>10 chars, not generic).
        """
        if not feedback or len(feedback.strip()) < 10:
            return

        # Skip generic/non-actionable feedback
        generic = {"plan rejected by user", "plan revision requested by user", "plan approval interrupted"}
        if feedback.strip().lower() in generic:
            return

        dedup_key = feedback[:50].lower().strip()
        if dedup_key in self._saved_corrections:
            return
        self._saved_corrections.add(dedup_key)

        self._save_feedback(feedback, f"Plan feedback: {feedback[:60]}")

    def check_tool_result(
        self,
        tool_name: str,
        params: dict,
        output: str,
        success: bool,
    ) -> None:
        """
        检查工具执行结果是否包含值得记忆的模式。
        成功的构建/测试命令 → 保存为 project 类型记忆。
        """
        if tool_name != "shell" or not success:
            return

        cmd = params.get("cmd", "") or params.get("command", "")
        if not cmd:
            return

        # 检测是否为构建/测试命令
        is_build_cmd = any(p.search(cmd) for p in _BUILD_CMD_PATTERNS)
        if not is_build_cmd:
            return

        # 检测是否成功
        output_lower = output.lower()
        is_success = any(ind in output_lower for ind in _SUCCESS_INDICATORS)
        if not is_success:
            return

        self._save_build_command(cmd)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _save_feedback(self, full_message: str, matched_text: str) -> None:
        """保存用户修正为 feedback 记忆。"""
        from memory.models import Anchor, Memory, MemoryMetadata

        anchors = self._anchors_from_text(full_message)
        mem_type = "feedback"
        name = self._generate_name(matched_text, prefix=mem_type)

        content = (
            f"User correction:\n"
            f"> {full_message.strip()}\n\n"
            f"**Rule:** {matched_text}\n\n"
            f"**How to apply:** Follow this guidance in future interactions."
        )

        memory = Memory(
            name=name,
            description=f"User feedback: {matched_text[:60]}",
            content=content,
            metadata=MemoryMetadata(type=mem_type),
            anchors=anchors,
        )

        if self._store.write_memory(memory):
            logger.info("Proactive memory saved: %s (%s)", name, mem_type)

    def _save_build_command(self, cmd: str) -> None:
        """保存成功的构建命令为 project 记忆。"""
        from memory.models import Memory, MemoryMetadata

        cmd_key = cmd.strip().lower()
        if cmd_key in self._saved_commands:
            return
        self._saved_commands.add(cmd_key)

        existing = self._store.read_memory("build-commands")
        if existing and cmd in existing.content:
            return

        if existing:
            updated_content = existing.content.rstrip() + f"\n- `{cmd}`"
            memory = Memory(
                name="build-commands",
                description=existing.description,
                content=updated_content,
                metadata=existing.metadata,
            )
        else:
            memory = Memory(
                name="build-commands",
                description="Build, test, and lint commands for this project",
                content=f"## Build Commands\n\n- `{cmd}`",
                metadata=MemoryMetadata(type="project"),
            )

        if self._store.write_memory(memory):
            logger.info("Proactive memory saved: build-commands (project)")

    @staticmethod
    def _anchors_from_text(text: str) -> "list[Anchor]":
        """从文本中提取文件路径作为 file anchors。"""
        from memory.models import Anchor
        file_re = re.compile(r"[A-Za-z0-9_.\-/\\]+\.[A-Za-z0-9_]+")
        anchors: list[Anchor] = []
        seen: set[str] = set()
        for match in file_re.findall(text):
            path = match.replace("\\", "/").strip(".,:;()[]{}<>`'\"")
            if not path or path in seen or len(path) < 4:
                continue
            anchors.append(Anchor(kind="file", path=path))
            seen.add(path)
        return anchors

    @staticmethod
    def _generate_name(text: str, prefix: str = "procedural") -> str:
        """从文本生成 kebab-case 记忆名。"""
        import hashlib
        # 取关键词
        words = re.findall(r"[a-zA-Z]+|[一-鿿]+", text.lower())
        slug_words = [w for w in words[:4] if len(w) > 1]
        if slug_words:
            slug = "-".join(slug_words)[:30]
        else:
            slug = hashlib.md5(text.encode()).hexdigest()[:8]
        return f"{prefix}-{slug}"
