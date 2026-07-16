"""
memory/extractor.py

自动记忆提取管线。

阶段 2 遵循 Mem0 Extract / Generative Agents reflection 思路：
把成功任务的事件摘要交给 LLM 做结构化抽取，产出高置信记忆候选。
规则提取只作为显式开启的无 LLM 降级，不参与默认主路径。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from llm.base import LLMMessage
from memory.models import Anchor, Memory, MemoryMetadata, normalize_memory_type

if TYPE_CHECKING:
    from agent.event_log import EventLog
    from agent.task import Task
    from llm.base import LLMBackend
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)

_ALLOWED_ANCHOR_KINDS = {"file", "symbol", "task"}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class MemoryCandidate:
    """自动提取出的候选记忆。"""
    type: str
    name: str
    description: str
    content: str
    anchors: list[Anchor] = field(default_factory=list)
    confidence: str = "high"

    def to_memory(self) -> Memory:
        return Memory(
            name=self.name,
            description=self.description,
            content=self.content,
            metadata=MemoryMetadata(type=normalize_memory_type(self.type)),
            anchors=self.anchors,
        )


class MemoryExtractor:
    """从成功任务日志中用 LLM reflection 抽取长期记忆候选。"""

    def __init__(
        self,
        backend: "LLMBackend | None" = None,
        *,
        enable_rule_fallback: bool = False,
    ) -> None:
        self._backend = backend
        self._enable_rule_fallback = enable_rule_fallback

    def extract(self, task: "Task", log: "EventLog", summary: str) -> list[MemoryCandidate]:
        try:
            if self._backend is not None:
                return self._extract_with_llm(task, log, summary)
            if self._enable_rule_fallback:
                return self._extract_rule_fallback(task, log, summary)
            return []
        except Exception as exc:
            logger.warning("Memory extraction failed: %s", exc)
            return []

    def write_success_memories(
        self,
        task: "Task",
        log: "EventLog",
        summary: str,
        store: "MemoryStore | None",
        external_store: Any = None,
        skip_auto_extract: bool = False,
    ) -> int:
        """提取并写入成功任务记忆；任何失败都不影响主流程。"""
        if store is None or skip_auto_extract:
            return 0
        written = 0
        for candidate in self.extract(task, log, summary):
            if candidate.confidence == "low":
                continue
            try:
                consolidate = getattr(store, "consolidate", None)
                if callable(consolidate):
                    action = consolidate(
                        candidate,
                        external_store=external_store,
                        backend=self._backend,
                    )
                    if action != "NOOP":
                        written += 1
                elif store.write_memory(candidate.to_memory()):
                    written += 1
            except Exception as exc:
                logger.warning("Failed to write extracted memory %s: %s", candidate.name, exc)
        return written

    def _extract_with_llm(self, task: "Task", log: "EventLog", summary: str) -> list[MemoryCandidate]:
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "You are a memory extraction module for a coding agent. "
                    "Extract only durable, useful memories from a completed task. "
                    "Follow Mem0-style extract semantics and Generative Agents-style reflection: "
                    "derive concise memories from observations, not a generic conversation summary. "
                    "Return ONLY valid JSON with this shape: "
                    "{\"memories\":[{\"type\":\"user|feedback|project|reference\","
                    "\"name\":\"kebab-case-slug\",\"description\":\"one line\","
                    "\"content\":\"markdown\",\"confidence\":\"high|medium|low\","
                    "\"anchors\":[{\"kind\":\"file|symbol|task\",\"path\":\"...\","
                    "\"name\":\"...\",\"value\":\"...\"}]}]}. "
                    "Use user for role/preferences, feedback for corrections/rules, "
                    "project for architecture/decisions/build commands, reference for external pointers. "
                    "Only save if it will still be valuable after 1 week AND cannot be derived from the codebase. "
                    "Do NOT save code patterns, file structure, git history/recent changes, fixed bug solutions, "
                    "content already in CLAUDE.md, temporary debug steps, or current conversation state. "
                    "Feedback memories SHOULD include file or symbol anchors when possible "
                    "(kind='file' with path, or kind='symbol' with name) for precise triggering. "
                    "If no specific file/symbol can be identified, use project type instead."
                ),
            ),
            LLMMessage(role="user", content=self._build_extraction_context(task, log, summary)),
        ]
        response = self._backend.complete(messages, tools=[])
        raw = response.action.message or response.action.thought or response.raw_content
        return self._parse_candidates(raw)

    def _build_extraction_context(self, task: "Task", log: "EventLog", summary: str) -> str:
        tool_calls: list[str] = []
        observations: list[str] = []
        for event in log.replay():
            from agent.task import EventType
            if event.event_type is EventType.ACTION:
                for tool_call in event.payload.get("action", {}).get("tool_calls") or []:
                    name = tool_call.get("name", "")
                    params = tool_call.get("params", {})
                    tool_calls.append(f"- {name}: {params}")
            elif event.event_type is EventType.OBSERVATION:
                observation = event.payload.get("observation", {})
                status = observation.get("status", "")
                tool_name = observation.get("tool_name", "")
                output = (observation.get("output") or observation.get("error") or "").strip()
                if output:
                    output = output[:500].replace("\n", " ")
                observations.append(f"- {tool_name} [{status}]: {output}")

        return "\n".join([
            "Completed task context:",
            f"Task: {task.description}",
            f"Final summary: {summary}",
            "",
            "Tool calls:",
            "\n".join(tool_calls[-12:]) or "- none",
            "",
            "Observations:",
            "\n".join(observations[-12:]) or "- none",
            "",
            "Extract durable memories only. If nothing should be remembered, return {\"memories\":[]}.",
        ])

    def _parse_candidates(self, raw: str) -> list[MemoryCandidate]:
        data = self._load_json(raw)
        memories = data.get("memories", []) if isinstance(data, dict) else data
        if not isinstance(memories, list):
            return []

        candidates: list[MemoryCandidate] = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            mem_type = normalize_memory_type(item.get("type"))
            description = str(item.get("description") or "").strip()
            content = str(item.get("content") or "").strip()
            if not description or not content:
                continue
            confidence = str(item.get("confidence") or "medium").strip().lower()
            if confidence not in _ALLOWED_CONFIDENCE:
                confidence = "medium"
            anchors = self._parse_anchors(item.get("anchors") or [])
            name = self._normalize_name(str(item.get("name") or ""), description, content)
            candidates.append(MemoryCandidate(
                type=mem_type,
                name=name,
                description=description,
                content=content,
                anchors=anchors,
                confidence=confidence,
            ))
        return candidates

    @staticmethod
    def _load_json(raw: str) -> Any:
        """Parse structured LLM output.  Requires a backend that provides structured
        responses (native tool_use or structured_output); the caller must pass
        tools=[] for text-only backends.

        Claude Code pattern: native tool_use blocks exclusively, zero regex.
        """
        import json
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"memories": []}

    @staticmethod
    def _parse_anchors(raw_anchors: list[Any]) -> list[Anchor]:
        anchors: list[Anchor] = []
        for raw_anchor in raw_anchors:
            if not isinstance(raw_anchor, dict):
                continue
            kind = str(raw_anchor.get("kind") or "").strip()
            if kind not in _ALLOWED_ANCHOR_KINDS:
                continue
            anchors.append(Anchor(
                kind=kind,
                path=raw_anchor.get("path") or None,
                name=raw_anchor.get("name") or None,
                value=raw_anchor.get("value") or None,
            ))
        return anchors

    def _extract_rule_fallback(self, task: "Task", log: "EventLog", summary: str) -> list[MemoryCandidate]:
        if not summary.strip():
            return []
        return [MemoryCandidate(
            type="project",
            name=self._slug(f"{task.description} {summary}"),
            description=f"Completed task: {task.description[:80]}",
            content=f"Task completed successfully.\n\n**Task:** {task.description}\n\n**Outcome:** {summary}",
            anchors=[],
            confidence="medium",
        )]

    @classmethod
    def _normalize_name(cls, raw_name: str, description: str, content: str) -> str:
        # Validate kebab-case: lowercase letters, digits, hyphens
        cleaned = raw_name.strip().lower()
        if cleaned and all(c.isalnum() or c == "-" for c in cleaned) and not cleaned.startswith("-") and not cleaned.endswith("-") and "--" not in cleaned:
            return cleaned[:80]
        return cls._slug(f"{description} {content}")

    @staticmethod
    def _slug(text: str) -> str:
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
        return f"memory-{digest}"
