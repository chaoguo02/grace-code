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
    timeout_seconds: float = 60.0


@dataclass
class AgentCfg:
    max_steps: int = 40
    budget_tokens: int = 80_000
    log_dir: str = ""


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
class MemoryConfig:
    enabled: bool = True
    directory: str = ""
    max_index_lines: int = 50
    auto_memory: bool = True
    selector_enabled: bool = True
    selector_model: str = ""


@dataclass
class CodeIndexConfig:
    enabled: bool = True
    max_chunk_lines: int = 100
    min_chunk_lines: int = 2


@dataclass
class PlanCfg:
    enable_replan: bool = False
    max_replans: int = 1
    allow_parallel_verification: bool = False
    allow_parallel_commands: bool = False


@dataclass
class MultiAgentCfg:
    worker_model: str = ""
    worker_provider: str = ""
    max_parallel_executors: int = 3
    coordinator_budget_ratio: float = 0.30
    sub_agent_budget_ratio: float = 0.70
    max_retries: int = 2
    coordinator_max_steps: int = 25


@dataclass
class ContextConfig:
    repo_map_budget: int = 8_000
    history_window: int = 20
    project_rules_file: str = ".forge-agent/rules.md"
    code_index: CodeIndexConfig = field(default_factory=CodeIndexConfig)
    # Phase 2: 预算分离
    request_budget_tokens: int = 70_000      # 单次 LLM request 输入上下文目标
    session_compact_tokens: int = 30_000     # shared_history 超过此值触发自动压缩
    auto_compact_after_round: bool = True    # 是否在每轮结束后检查自动压缩
    compact_every_rounds: int = 3            # 每 N 轮强制检查压缩
    artifact_threshold_tokens: int = 2_000   # 工具输出超过此值时 artifact 化
    artifact_storage_dir: str = ""


@dataclass
class HitlConfig:
    enabled: bool = True
    min_risk_for_confirm: str = "medium"
    policy_file: str = ".forge-agent/hitl/policies.yaml"
    learn_threshold: int = 3
    settings_file: str = ".forge-agent/settings.json"
    default_mode: str = "ask_edits"


@dataclass
class LangfuseConfig:
    public_key: str = ""
    secret_key: str = ""
    base_url: str = "https://cloud.langfuse.com"


@dataclass
class ObservabilityConfig:
    enabled: bool = False
    provider: str = "langfuse"
    environment: str = "development"
    flush_on_exit: bool = True
    capture_prompts: bool = True
    capture_tool_outputs: bool = True
    capture_llm_outputs: bool = True
    mask_sensitive_data: bool = True
    sample_rate: float = 1.0
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)


@dataclass
class PromptConfig:
    source: str = "local"  # local | langfuse | hybrid
    label: str = "production"
    version: int | None = None
    namespace: str = "forge"
    cache_ttl_seconds: int = 300
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentCfg = field(default_factory=AgentCfg)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    plan: PlanCfg = field(default_factory=PlanCfg)
    multi_agent: MultiAgentCfg = field(default_factory=MultiAgentCfg)
    context: ContextConfig = field(default_factory=ContextConfig)
    hitl: HitlConfig = field(default_factory=HitlConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    prompts: PromptConfig = field(default_factory=PromptConfig)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)


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
    memory_raw = data.get("memory", {})
    plan_raw = data.get("plan", {})
    multi_agent_raw = data.get("multi_agent", {})
    context_raw = data.get("context", {})
    hitl_raw = data.get("hitl", {})
    observability_raw = data.get("observability", {})
    langfuse_raw = observability_raw.get("langfuse", {})
    prompts_raw = data.get("prompts", {})
    prompts_langfuse_raw = prompts_raw.get("langfuse", {})

    llm = LLMConfig(
        provider=llm_raw.get("provider", "") or "deepseek",
        model=llm_raw.get("model", "") or "deepseek/deepseek-v4-flash",
        api_key=llm_raw.get("api_key", "") or "",
        base_url=llm_raw.get("base_url", "") or "",
        max_tokens=int(llm_raw.get("max_tokens", 8192)),
        timeout_seconds=float(llm_raw.get("timeout_seconds", 60.0)),
    )

    agent = AgentCfg(
        max_steps=int(agent_raw.get("max_steps", 40)),
        budget_tokens=int(agent_raw.get("budget_tokens", 80_000)),
        log_dir=agent_raw.get("log_dir", ""),
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

    memory = MemoryConfig(
        enabled=bool(memory_raw.get("enabled", True)),
        directory=memory_raw.get("directory", ""),
        max_index_lines=int(memory_raw.get("max_index_lines", 50)),
        auto_memory=bool(memory_raw.get("auto_memory", True)),
        selector_enabled=bool(memory_raw.get("selector_enabled", True)),
        selector_model=memory_raw.get("selector_model", ""),
    )

    plan = PlanCfg(
        enable_replan=bool(plan_raw.get("enable_replan", False)),
        max_replans=int(plan_raw.get("max_replans", 1)),
        allow_parallel_verification=bool(plan_raw.get("allow_parallel_verification", False)),
        allow_parallel_commands=bool(plan_raw.get("allow_parallel_commands", False)),
    )

    multi_agent = MultiAgentCfg(
        worker_model=multi_agent_raw.get("worker_model", "") or "",
        worker_provider=multi_agent_raw.get("worker_provider", "") or "",
        max_parallel_executors=int(multi_agent_raw.get("max_parallel_executors", 3)),
        coordinator_budget_ratio=float(multi_agent_raw.get("coordinator_budget_ratio", 0.30)),
        sub_agent_budget_ratio=float(multi_agent_raw.get("sub_agent_budget_ratio", 0.70)),
        max_retries=int(multi_agent_raw.get("max_retries", 2)),
        coordinator_max_steps=int(multi_agent_raw.get("coordinator_max_steps", 25)),
    )

    code_index_raw = context_raw.get("code_index", {})
    code_index = CodeIndexConfig(
        enabled=bool(code_index_raw.get("enabled", True)),
        max_chunk_lines=int(code_index_raw.get("max_chunk_lines", 100)),
        min_chunk_lines=int(code_index_raw.get("min_chunk_lines", 3)),
    )

    context = ContextConfig(
        repo_map_budget=int(context_raw.get("repo_map_budget", 8_000)),
        history_window=int(context_raw.get("history_window", 20)),
        code_index=code_index,
        request_budget_tokens=int(context_raw.get("request_budget_tokens", 70_000)),
        session_compact_tokens=int(context_raw.get("session_compact_tokens", 30_000)),
        auto_compact_after_round=bool(context_raw.get("auto_compact_after_round", True)),
        compact_every_rounds=int(context_raw.get("compact_every_rounds", 3)),
        artifact_threshold_tokens=int(context_raw.get("artifact_threshold_tokens", 2_000)),
        artifact_storage_dir=context_raw.get("artifact_storage_dir", ""),
    )

    hitl = HitlConfig(
        enabled=bool(hitl_raw.get("enabled", True)),
        min_risk_for_confirm=hitl_raw.get("min_risk_for_confirm", "medium"),
        policy_file=hitl_raw.get("policy_file", ".forge-agent/hitl/policies.yaml"),
        learn_threshold=int(hitl_raw.get("learn_threshold", 3)),
    )

    observability = ObservabilityConfig(
        enabled=bool(observability_raw.get("enabled", False)),
        provider=observability_raw.get("provider", "langfuse") or "langfuse",
        environment=observability_raw.get("environment", "development") or "development",
        flush_on_exit=bool(observability_raw.get("flush_on_exit", True)),
        capture_prompts=bool(observability_raw.get("capture_prompts", True)),
        capture_tool_outputs=bool(observability_raw.get("capture_tool_outputs", True)),
        capture_llm_outputs=bool(observability_raw.get("capture_llm_outputs", True)),
        mask_sensitive_data=bool(observability_raw.get("mask_sensitive_data", True)),
        sample_rate=float(observability_raw.get("sample_rate", 1.0)),
        langfuse=LangfuseConfig(
            public_key=langfuse_raw.get("public_key", "") or "",
            secret_key=langfuse_raw.get("secret_key", "") or "",
            base_url=langfuse_raw.get("base_url", "https://cloud.langfuse.com") or "https://cloud.langfuse.com",
        ),
    )

    prompt_version_raw = prompts_raw.get("version")
    prompt_version: int | None
    if prompt_version_raw in ("", None):
        prompt_version = None
    else:
        prompt_version = int(prompt_version_raw)

    prompts = PromptConfig(
        source=prompts_raw.get("source", "local") or "local",
        label=prompts_raw.get("label", "production") or "production",
        version=prompt_version,
        namespace=prompts_raw.get("namespace", "forge") or "forge",
        cache_ttl_seconds=int(prompts_raw.get("cache_ttl_seconds", 300)),
        langfuse=LangfuseConfig(
            public_key=prompts_langfuse_raw.get("public_key", "") or "",
            secret_key=prompts_langfuse_raw.get("secret_key", "") or "",
            base_url=prompts_langfuse_raw.get("base_url", "https://cloud.langfuse.com") or "https://cloud.langfuse.com",
        ),
    )

    mcp_servers: dict[str, dict[str, Any]] = data.get("mcp_servers", {}) or {}

    return AppConfig(
        llm=llm, agent=agent, tools=tools,
        memory=memory, plan=plan, multi_agent=multi_agent,
        context=context, hitl=hitl, observability=observability, prompts=prompts,
        mcp_servers=mcp_servers,
    )


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
