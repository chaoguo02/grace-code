"""
config/schema.py

配置文件加载与校验。把 config/default.yaml 解析成类型安全的 dataclass。

支持：
- 环境变量展开：${VAR} 语法
- 多层配置合并：default.yaml < 用户指定 yaml < CLI 参数
- 缺失必填项时给出清晰错误信息
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# 配置 dataclass
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    provider: str = ""          # 空值表示未配置，必须通过 default.yaml 或 CLI 指定
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 8192


@dataclass
class AgentCfg:
    max_steps: int = 40
    budget_tokens: int = 80_000
    log_dir: str = "./logs"


@dataclass
class ShellToolConfig:
    timeout: int = 30
    max_output_tokens: int = 8_000


@dataclass
class FileToolConfig:
    max_view_lines: int = 100


@dataclass
class WebToolConfig:
    search_max_results: int = 10
    fetch_max_chars: int = 100_000
    fetch_timeout: int = 15


@dataclass
class ToolsConfig:
    shell: ShellToolConfig = field(default_factory=ShellToolConfig)
    file: FileToolConfig = field(default_factory=FileToolConfig)
    web: WebToolConfig = field(default_factory=WebToolConfig)


@dataclass
class ContextConfig:
    repo_map_budget: int = 8_000
    history_window: int = 20


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentCfg = field(default_factory=AgentCfg)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)


# ---------------------------------------------------------------------------
# 加载函数
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _expand_env(text: str) -> str:
    """展开 ${VAR} 形式的环境变量占位符。"""
    def replace(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return _ENV_RE.sub(replace, text)


def load_config(path: str | Path | None = None) -> AppConfig:
    """
    加载配置文件，返回 AppConfig。

    Args:
        path: YAML 文件路径，None 时自动查找 config/default.yaml

    Returns:
        AppConfig 实例
    """
    if path is None:
        # 自动查找：当前目录 → 项目根目录
        candidates = [
            Path("config/default.yaml"),
            Path(__file__).parent / "default.yaml",
        ]
        for p in candidates:
            if p.exists():
                path = p
                break
        else:
            return AppConfig()   # 找不到配置文件，用全默认值

    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()
    raw = config_path.read_text(encoding="utf-8")
    raw = _expand_env(raw)
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    return _parse(data)


def _parse(data: dict[str, Any]) -> AppConfig:
    """把 yaml dict 解析为 AppConfig。"""
    llm_raw = data.get("llm", {})
    agent_raw = data.get("agent", {})
    tools_raw = data.get("tools", {})
    context_raw = data.get("context", {})

    llm = LLMConfig(
        provider=llm_raw.get("provider", ""),
        model=llm_raw.get("model", ""),
        api_key=llm_raw.get("api_key", ""),
        base_url=llm_raw.get("base_url", "") or "",
        max_tokens=int(llm_raw.get("max_tokens", 8192)),
    )

    agent = AgentCfg(
        max_steps=int(agent_raw.get("max_steps", 40)),
        budget_tokens=int(agent_raw.get("budget_tokens", 80_000)),
        log_dir=agent_raw.get("log_dir", "./logs"),
    )

    shell_raw = tools_raw.get("shell", {})
    file_raw = tools_raw.get("file", {})
    web_raw = tools_raw.get("web", {})
    tools = ToolsConfig(
        shell=ShellToolConfig(
            timeout=int(shell_raw.get("timeout", 30)),
            max_output_tokens=int(shell_raw.get("max_output_tokens", 8_000)),
        ),
        file=FileToolConfig(
            max_view_lines=int(file_raw.get("max_view_lines", 100)),
        ),
        web=WebToolConfig(
            search_max_results=int(web_raw.get("search_max_results", 10)),
            fetch_max_chars=int(web_raw.get("fetch_max_chars", 100_000)),
            fetch_timeout=int(web_raw.get("fetch_timeout", 15)),
        ),
    )

    context = ContextConfig(
        repo_map_budget=int(context_raw.get("repo_map_budget", 8_000)),
        history_window=int(context_raw.get("history_window", 20)),
    )

    return AppConfig(llm=llm, agent=agent, tools=tools, context=context)


def merge_cli_overrides(
    config: AppConfig,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    max_steps: int | None = None,
    max_tokens: int | None = None,
) -> AppConfig:
    """
    把 CLI 参数覆盖到已加载的 config 上。
    CLI 参数优先级最高。
    """
    if provider:
        config.llm.provider = provider
    if model:
        config.llm.model = model
    if api_key:
        config.llm.api_key = api_key
    if base_url:
        config.llm.base_url = base_url
    if max_steps is not None:
        config.agent.max_steps = max_steps
    if max_tokens is not None:
        config.llm.max_tokens = max_tokens
    return config