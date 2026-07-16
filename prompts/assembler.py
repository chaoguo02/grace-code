"""
prompts/assembler.py

Prompt 分层架构核心：PromptAssembler。

职责：
- 从 prompts/ 目录加载 .md 文件
- 三层覆盖：内置 -> ~/.forge-agent/prompts/ -> .forge-agent/prompts/
- 支持 local / langfuse / hybrid 三种 prompt 来源
- 模板变量替换 ({repo_path}, {tool_descriptions}, ...)
- 返回渲染后的字符串与可观测元数据
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.schema import PromptConfig

logger = logging.getLogger(__name__)


class _SafeDict(dict):
    """format_map 用的安全字典，未知 key 保留原样 {key}。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class PromptRenderResult:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class _LangfusePromptProvider:
    def __init__(self, config: PromptConfig) -> None:
        self._config = config
        self._client: Any | None = None
        self._cache: dict[str, tuple[float, Any]] = {}

    def render(self, relative_path: str, variables: dict[str, Any]) -> PromptRenderResult:
        prompt_name = self._prompt_name_for(relative_path)
        prompt = self._get_prompt(prompt_name)
        compiled = prompt.compile(**variables)
        return PromptRenderResult(
            text=self._coerce_compiled_prompt(compiled),
            metadata={
                "source": "langfuse",
                "path": relative_path,
                "prompt_name": getattr(prompt, "name", prompt_name),
                "prompt_version": getattr(prompt, "version", self._config.version),
                "prompt_label": self._config.label if self._config.version is None else None,
                "prompt_labels": getattr(prompt, "labels", None),
                "namespace": self._config.namespace,
            },
        )

    def clear_cache(self) -> None:
        self._cache.clear()

    def _get_prompt(self, prompt_name: str) -> Any:
        cache_key = f"{prompt_name}|{self._config.label}|{self._config.version}"
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self._config.cache_ttl_seconds:
            return cached[1]

        client = self._get_client()
        kwargs: dict[str, Any] = {"type": "text"}
        if self._config.version is not None:
            kwargs["version"] = self._config.version
        elif self._config.label:
            kwargs["label"] = self._config.label
        prompt = client.get_prompt(prompt_name, **kwargs)
        self._cache[cache_key] = (now, prompt)
        return prompt

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        public_key = self._config.langfuse.public_key.strip()
        secret_key = self._config.langfuse.secret_key.strip()
        base_url = self._config.langfuse.base_url.strip()
        if not public_key or not secret_key:
            raise RuntimeError("Langfuse prompt source requires public_key and secret_key")

        os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
        os.environ["LANGFUSE_SECRET_KEY"] = secret_key
        if base_url:
            os.environ["LANGFUSE_BASE_URL"] = base_url

        try:
            from langfuse import get_client
        except ImportError as exc:
            raise RuntimeError("Langfuse package is not installed") from exc

        self._client = get_client()
        return self._client

    def _prompt_name_for(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        if normalized.endswith(".md"):
            normalized = normalized[:-3]
        namespace = self._config.namespace.strip("/")
        return f"{namespace}/{normalized}" if namespace else normalized

    @staticmethod
    def _coerce_compiled_prompt(compiled: Any) -> str:
        if isinstance(compiled, str):
            return compiled
        if isinstance(compiled, list):
            parts: list[str] = []
            for item in compiled:
                if isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    parts.append(f"[{role}]\n{content}")
                else:
                    parts.append(str(item))
            return "\n\n".join(parts)
        return str(compiled)


class PromptAssembler:
    """
    三层覆盖的 Prompt 文件加载与渲染。

    查找顺序（高优先级 -> 低优先级）：
    1. 项目级: {project_dir}/.forge-agent/prompts/
    2. 用户级: ~/.forge-agent/prompts/
    3. 内置:   {package}/prompts/
    """

    BUILTIN_DIR = Path(__file__).parent
    USER_DIR = Path.home() / ".forge-agent" / "prompts"

    def __init__(
        self,
        project_dir: str | Path | None = None,
        config: PromptConfig | None = None,
    ):
        self._project_dir: Path | None = None
        if project_dir:
            self._project_dir = Path(project_dir) / ".forge-agent" / "prompts"
        self._config = config or PromptConfig()
        self._cache: dict[str, str] = {}
        self._langfuse_provider = _LangfusePromptProvider(self._config)

    def resolve(self, relative_path: str) -> str:
        """从本地三层目录中读取 prompt 原文。"""
        if relative_path in self._cache:
            return self._cache[relative_path]

        content = self._load_from_layers(relative_path)
        self._cache[relative_path] = content
        return content

    def render(self, relative_path: str, **variables: Any) -> str:
        return self.render_result(relative_path, **variables).text

    def render_result(self, relative_path: str, **variables: Any) -> PromptRenderResult:
        source = (self._config.source or "local").lower()
        if source == "local":
            return self._render_local(relative_path, variables)
        if source == "langfuse":
            return self._langfuse_provider.render(relative_path, variables)
        if source == "hybrid":
            try:
                return self._langfuse_provider.render(relative_path, variables)
            except Exception as exc:
                logger.warning("Langfuse prompt fetch failed for %s, falling back to local: %s", relative_path, exc)
                return self._render_local(relative_path, variables)
        raise ValueError(f"Unknown prompt source: {self._config.source!r}")

    def render_system_core(
        self,
        repo_path: str,
        tools: list,
        repo_summary: str | None = None,
    ) -> str:
        return self.render_system_core_result(repo_path, tools, repo_summary).text

    def render_system_core_result(
        self,
        repo_path: str,
        tools: list,
        repo_summary: str | None = None,
    ) -> PromptRenderResult:
        tool_descriptions = self._format_tool_descriptions(tools)
        tool_contract_rules = self._build_tool_contract_rules(tools)
        platform_info = self._build_platform_info()
        summary = repo_summary or "(Repository summary not yet available - use find_files and file_read to explore)"
        return self.render_result(
            "base.md",
            repo_path=repo_path,
            repo_summary=summary,
            tool_descriptions=tool_descriptions,
            tool_contract_rules=tool_contract_rules,
            platform_info=platform_info,
        )

    def render_mode_prompt(self, mode: str, **variables: Any) -> str:
        path = f"modes/{mode}.md"
        try:
            return self.render(path, **variables)
        except FileNotFoundError:
            return ""

    def render_reflection(self, kind: str, **variables: Any) -> str:
        return self.render(f"reflection/{kind}.md", **variables)

    def render_agent_prompt(self, template: str, **variables: Any) -> str:
        return self.render(f"agents/{template}.md", **variables)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._langfuse_provider.clear_cache()

    def _render_local(self, relative_path: str, variables: dict[str, Any]) -> PromptRenderResult:
        raw = self.resolve(relative_path)
        text = raw if not variables else raw.format_map(_SafeDict(variables))
        return PromptRenderResult(
            text=text,
            metadata={
                "source": "local",
                "path": relative_path,
                "prompt_name": self._local_prompt_name(relative_path),
                "prompt_label": None,
                "prompt_version": None,
                "namespace": self._config.namespace,
            },
        )

    def _load_from_layers(self, relative_path: str) -> str:
        if self._project_dir:
            project_path = self._project_dir / relative_path
            if project_path.is_file():
                logger.debug("Prompt override (project): %s", project_path)
                return project_path.read_text(encoding="utf-8")

        user_path = self.USER_DIR / relative_path
        if user_path.is_file():
            logger.debug("Prompt override (user): %s", user_path)
            return user_path.read_text(encoding="utf-8")

        builtin_path = self.BUILTIN_DIR / relative_path
        if builtin_path.is_file():
            return builtin_path.read_text(encoding="utf-8")

        raise FileNotFoundError(
            f"Prompt file not found in any layer: {relative_path}\n"
            f"  Searched: project={self._project_dir}, user={self.USER_DIR}, builtin={self.BUILTIN_DIR}"
        )

    def _local_prompt_name(self, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        if normalized.endswith(".md"):
            normalized = normalized[:-3]
        namespace = self._config.namespace.strip("/")
        return f"{namespace}/{normalized}" if namespace else normalized

    @staticmethod
    def _build_platform_info() -> str:
        """Declare the platform truth to the LLM — control plane, not secret translation."""
        import platform as _platform, os as _os
        system = _platform.system()
        if system == "Windows":
            return (
                "## Platform\n"
                "You are running on **Windows**. Available shell: **PowerShell**.\n"
                "- Use PowerShell commands. Do NOT use Linux commands (wc, grep, find, cat, ls).\n"
                "- wc → `(Get-Content file).Count`\n"
                "- grep → `Select-String`\n"
                "- find → `Get-ChildItem -Recurse`\n"
                "- cat → `Get-Content`\n"
                "- ls → `dir` or `Get-ChildItem`\n"
                "- which → `where` or `Get-Command`"
            )
        return (
            "## Platform\n"
            "You are running on **Linux/macOS**. Available shell: **bash**.\n"
        )

    @staticmethod
    def _format_tool_descriptions(tools: list) -> str:
        if not tools:
            return "(no tools available)"
        sorted_tools = sorted(tools, key=lambda t: t.name)
        lines = [f"- **{tool.name}**: {tool.description}" for tool in sorted_tools]
        return "\n".join(lines)

    @staticmethod
    def _build_tool_contract_rules(tools: list) -> str:
        """Generate mandatory tool usage rules from schema metadata.

        When a tool's schema changes (e.g., shell switched from 'cmd' string
        to 'command'+'args' array), this contract is automatically reflected
        in the system prompt. No hand-maintained prompt text needed.
        """
        rules = []
        for tool in tools:
            name = getattr(tool, "name", "")
            if name == "shell":
                rules.append(
                    "- **shell tool**: ALWAYS use `command` + `args` (NOT the deprecated `cmd` field). "
                    "Each argument is a separate list element: `{\"command\": \"pytest\", \"args\": [\"--tb=short\"]}`. "
                    "Never embed flags or paths inside the `command` string."
                )
            # Other tools with contract requirements can be added here
        if rules:
            return "\n## CRITICAL TOOL USAGE RULES\n" + "\n".join(rules)
        return ""
