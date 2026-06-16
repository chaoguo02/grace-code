"""
entry/cli.py

命令行入口。

用法：
    # 直接传任务描述
    python -m entry.cli run --repo /path/to/repo --task "Fix the failing test"

    # 从文件读任务描述
    python -m entry.cli run --repo . --task-file task.txt

    # 覆盖模型
    python -m entry.cli run --repo . --task "fix it" --model deepseek-chat

    # 查看 event log 统计
    python -m entry.cli log show logs/abc123_20240101_120000.jsonl

安装为命令行工具后（pyproject.toml 里配置了 scripts）：
    agent run --repo . --task "fix it"
"""

from __future__ import annotations

import io
import logging
import os
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

# Windows 终端强制 UTF-8 输出（避免 GBK 编码错误）
if sys.platform == "win32":
    os.system("")  # 启用 VT100 转义序列支持
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 把项目根加入 path（直接跑脚本时需要）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 加载 .env 文件（项目根目录），已有环境变量不会被覆盖
load_dotenv(_ROOT / ".env")

from config.schema import load_config, merge_cli_overrides   # noqa: E402
from llm.router import create_backend_from_config            # noqa: E402

# 模块级 import（供 patch 使用）
from config.schema import load_config, merge_cli_overrides  # noqa: E402
from llm.router import create_backend_from_config           # noqa: E402


# ---------------------------------------------------------------------------
# 辅助：彩色输出
# ---------------------------------------------------------------------------

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


# ---------------------------------------------------------------------------
# 构建 agent 各组件
# ---------------------------------------------------------------------------

def _build_registry(cfg, confirm_callback=None, runtime=None, memory_store=None, external_store=None):
    """根据配置组装工具注册表。"""
    from tools.base import ToolRegistry
    from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool
    from tools.web_tool import WebSearchTool, WebFetchTool

    web_cfg = cfg.tools.web
    registry = (
        ToolRegistry()
        .register(ShellTool(confirm_callback=confirm_callback, runtime=runtime))
        .register(FileReadTool())
        .register(FileViewTool())
        .register(FileWriteTool())
        .register(SearchTextTool())
        .register(FindFilesTool())
        .register(FindSymbolTool())
        .register(PytestTool(runtime=runtime))
        .register(GitStatusTool(runtime=runtime))
        .register(GitDiffTool(runtime=runtime))
        .register(GitAddTool(runtime=runtime))
        .register(GitCommitTool(runtime=runtime))
        .register(WebSearchTool(max_results=web_cfg.search_max_results))
        .register(WebFetchTool(
            max_chars=web_cfg.fetch_max_chars,
            timeout=web_cfg.fetch_timeout,
        ))
    )

    # 注册记忆工具（如果提供了 MemoryStore）
    if memory_store is not None:
        from tools.memory_tool import (
            MemoryReadTool, MemoryWriteTool,
            MemoryListTool, MemoryDeleteTool,
        )
        registry \
            .register(MemoryReadTool(memory_store)) \
            .register(MemoryWriteTool(memory_store)) \
            .register(MemoryListTool(memory_store)) \
            .register(MemoryDeleteTool(memory_store))

    # 注册外部记忆搜索工具
    if external_store is not None:
        from tools.memory_tool import MemorySearchTool
        registry.register(MemorySearchTool(external_store))

    # 注册 MCP 工具（从配置中读取 mcp_servers 并连接）
    mcp_servers_cfg = getattr(cfg, "mcp_servers", {}) or {}
    if mcp_servers_cfg:
        logger = logging.getLogger("cli")
        logger.info("Connecting to MCP servers: %s", list(mcp_servers_cfg.keys()))
        try:
            from tools.mcp_client import create_manager_from_config
            manager = create_manager_from_config(mcp_servers_cfg)
            proxies = manager.connect_and_discover_sync()
            for proxy in proxies:
                registry.register(proxy)
            logger.info("MCP tools registered: %s", [p.name for p in proxies])
        except Exception as exc:
            logger.warning("Failed to connect MCP servers: %s", exc)
            # MCP 连接失败不阻塞 agent 启动，继续使用本地工具

    return registry


def _print_step(event) -> None:
    """实时打印单条 event。"""
    from agent.task import EventType
    etype = event.event_type
    payload = event.payload

    if etype == EventType.TASK_START:
        task = payload["task"]
        click.echo(bold(f"\n{'─'*60}"))
        click.echo(bold(f"  Task : {task['description'][:80]}"))
        click.echo(bold(f"  Repo : {task['repo_path']}"))
        click.echo(bold(f"{'─'*60}\n"))

    elif etype == EventType.ACTION:
        step = payload["step"]
        action = payload["action"]
        thought = action.get("thought", "")[:160]
        atype = action.get("action_type", "")
        tcs = action.get("tool_calls") or []
        click.echo(cyan(f"[Step {step}] {atype}"))
        if thought:
            click.echo(dim(f"  ↳ {thought}"))
        if tcs:
            # 显示第一个 tool call 的名称和参数（简洁模式）
            first = tcs[0]
            params_str = str(first["params"])[:100]
            count = len(tcs)
            label = f"  Tool: {first['name']}" + (f" (+{count-1} more)" if count > 1 else "")
            click.echo(f"{label}  params: {params_str}")

    elif etype == EventType.OBSERVATION:
        obs = payload["observation"]
        status = obs.get("status", "")
        tool = obs.get("tool_name", "")
        output = obs.get("output", "")
        if status == "success":
            click.echo(green(f"  ✓ [{tool}]"))
        else:
            click.echo(red(f"  ✗ [{tool}] {obs.get('error', '')}"))
        # 打印前 5 行输出
        for line in output.splitlines()[:5]:
            click.echo(dim(f"    {line}"))
        if len(output.splitlines()) > 5:
            click.echo(dim(f"    ... ({len(output.splitlines())-5} more lines)"))
        click.echo()

    elif etype == EventType.REFLECTION:
        click.echo(yellow(f"\n  ⟳ Reflection: {payload.get('reason', '')}\n"))

    elif etype == EventType.TASK_COMPLETE:
        click.echo(green(bold(f"\n✓ COMPLETE: {payload.get('summary', '')}\n")))

    elif etype == EventType.TASK_FAILED:
        click.echo(red(bold(f"\n✗ FAILED: {payload.get('reason', '')}\n")))


# ---------------------------------------------------------------------------
# CLI 主命令组
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--config", "-c",
    default=None,
    help="Path to config YAML file (default: config/default.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Coding Agent — autonomous code editing and bug fixing."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ---------------------------------------------------------------------------
# Multi-Agent config helper
# ---------------------------------------------------------------------------

def _merge_approval_cb(worktree_name: str, diff: str) -> bool:
    """HITL: 展示 worktree diff，请求用户确认合并。"""
    click.echo(click.style(f"\n  ─── Worktree '{worktree_name}' diff ───", fg="cyan"))
    if diff.strip():
        # 截断过长 diff
        lines = diff.splitlines()
        if len(lines) > 60:
            click.echo("\n".join(lines[:60]))
            click.echo(click.style(f"  ... ({len(lines) - 60} more lines)", dim=True))
        else:
            click.echo(diff)
    else:
        click.echo("  (no diff)")
    click.echo(click.style("  ─────────────────────────────────────", fg="cyan"))
    try:
        resp = input(f"  Merge '{worktree_name}' into main branch? [y/n] > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return resp in ("y", "yes", "")


def _build_multi_config(config, auto_approve: bool = False) -> "MultiAgentConfig":
    """从 AppConfig 构建 MultiAgentConfig。"""
    from agent.multi_agent import MultiAgentConfig
    ma = config.multi_agent
    return MultiAgentConfig(
        budget_ratio=(ma.coordinator_budget_ratio, ma.sub_agent_budget_ratio),
        max_agents=ma.max_retries + 6,
        coordinator_max_steps=ma.coordinator_max_steps,
        max_parallel=ma.max_parallel_executors,
        worker_model=ma.worker_model or None,
        worker_provider=ma.worker_provider or None,
        merge_approval_callback=None if auto_approve else _merge_approval_cb,
        log_dir=config.agent.log_dir,
    )


# ---------------------------------------------------------------------------
# run 子命令
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--task", "-t", default=None, help="Task description (natural language)")
@click.option("--task-file", "-f", default=None, help="Read task description from file")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--base-url", default=None, help="Override LLM base URL")
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--max-tokens", default=None, type=int, help="Override max output tokens")
@click.option("--stream", "-s", is_flag=True, default=True, help="Enable streaming output (default: on)")
@click.option("--confirm", is_flag=True, default=False, help="Ask confirmation before running dangerous shell commands")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--mode", default="auto", show_default=True, type=click.Choice(["react", "plan", "dag", "multi-agent", "auto"]), help="Agent mode: react, plan, dag, multi-agent, or auto")
@click.option("--auto-approve", is_flag=True, default=False, help="Auto-approve plans without user confirmation (plan mode only)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def run(
    ctx: click.Context,
    repo: str,
    task: str | None,
    task_file: str | None,
    model: str | None,
    provider: str | None,
    base_url: str | None,
    max_steps: int | None,
    max_tokens: int | None,
    stream: bool,
    confirm: bool,
    sandbox: bool,
    mode: str,
    auto_approve: bool,
    verbose: bool,
) -> None:
    """Run the coding agent on a repository."""
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # 加载配置
    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(
        config, provider=provider, model=model,
        base_url=base_url, max_steps=max_steps, max_tokens=max_tokens,
    )

    # 解析任务描述
    if task_file:
        description = Path(task_file).read_text(encoding="utf-8").strip()
    elif task:
        description = task
    else:
        click.echo(red("Error: provide --task or --task-file"), err=True)
        sys.exit(1)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    # 打印运行信息
    click.echo(bold(f"\nCoding Agent"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Max steps: {config.agent.max_steps}\n")

    # 构建各组件
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    confirm_cb = terminal_confirm if confirm else None
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))

    # 初始化记忆系统
    memory_store = None
    memory_context = None
    external_store = None
    if config.memory.enabled:
        from memory.store import TwoTierMemoryStore
        from memory.context import MemoryContext
        from memory.external_store import ExternalMemoryStore
        from memory.indexer import MemoryIndexer
        from memory.retriever import ProactiveRetriever

        external_store = ExternalMemoryStore()
        indexer = MemoryIndexer(external_store)
        memory_store = TwoTierMemoryStore(
            repo_path=str(repo_path),
            memory_dir=config.memory.directory or None,
            max_index_lines=config.memory.max_index_lines,
            indexer=indexer,
        )
        retriever = ProactiveRetriever(external_store, max_chunks=5, max_tokens=2000)
        memory_context = MemoryContext(
            store=memory_store,
            max_lines=config.memory.max_index_lines,
            enabled=config.memory.enabled,
            retriever=retriever,
        )

    registry = _build_registry(
        config,
        confirm_callback=confirm_cb,
        runtime=runtime,
        memory_store=memory_store,
        external_store=external_store,
    )

    from agent.core import AgentConfig
    from agent.event_log import EventLog, summarize_run
    from agent.task import Task
    from agent.factory import create_agent
    from entry.renderer import create_renderer
    try:
        from context.token_budget import is_tiktoken_available
    except ImportError:
        is_tiktoken_available = lambda: False

    # 创建渲染器
    rend = create_renderer(model=config.llm.model, mode=mode)

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        history_max_messages=config.context.history_window * 2,
        stream=stream,
        stream_callback=rend.stream_text if stream else None,
        thought_callback=rend.stream_thought if stream else None,
        confirm_dangerous=confirm,
        confirm_callback=confirm_cb,
    )
    # Plan 审批回调
    def _plan_approval_cb(plan_text: str) -> bool:
        if auto_approve:
            rend.on_plan_generated(plan_text)
            rend.on_plan_approved()
            return True
        rend.on_plan_generated(plan_text)
        try:
            resp = input("  [approve(y)/reject(n)] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            rend.on_plan_rejected()
            return False
        if resp in ("y", "yes", "approve", "a", ""):
            rend.on_plan_approved()
            return True
        rend.on_plan_rejected()
        return False

    multi_cfg = _build_multi_config(config, auto_approve=auto_approve) if mode == "multi-agent" else None
    agent = create_agent(
        mode, backend, registry, agent_config,
        task_description=description,
        plan_approval_callback=_plan_approval_cb,
        memory_context=memory_context,
        multi_config=multi_cfg,
    )
    click.echo(dim(f"  Mode    : {mode}"))

    task_obj = Task(
        description=description,
        repo_path=str(repo_path),
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    if verbose:
        click.echo(dim(
            f"  tiktoken: {'yes' if is_tiktoken_available() else 'no (char estimate)'}\n"
        ))

    # 运行
    t0 = time.time()
    with EventLog.create(task_obj, log_dir=config.agent.log_dir) as log:
        click.echo(dim(f"  Log: {log.path}\n"))

        # 实时事件输出（monkey-patch EventLog）
        from agent.task import EventType
        _original_append = log._append
        _last_tool = [""]

        def _live_append(event):
            _original_append(event)
            etype = event.event_type
            p = event.payload
            if etype == EventType.ACTION:
                action = p["action"]
                tcs = action.get("tool_calls") or []
                if tcs:
                    for tc in tcs:
                        _last_tool[0] = tc["name"]
                        rend.on_tool_call(p["step"], tc["name"], tc.get("params", {}))
                elif action.get("action_type") == "finish":
                    rend.on_finish(p["step"], action.get("message", ""))
                elif action.get("action_type") == "give_up":
                    rend.on_give_up(p["step"], action.get("message", ""))
            elif etype == EventType.OBSERVATION:
                obs = p["observation"]
                rend.on_observation(
                    p["step"],
                    obs.get("tool_name", _last_tool[0]),
                    obs.get("status", ""),
                    obs.get("output", ""),
                    obs.get("error"),
                )
            elif etype == EventType.REFLECTION:
                rend.on_reflection(p.get("reason", ""))

        log._append = _live_append
        result = agent.run(task_obj, log)

    elapsed = time.time() - t0

    # 打印结果
    click.echo(bold("─" * 60))
    status_str = green("SUCCESS") if result.is_success() else red(result.status.value.upper())
    click.echo(f"Status  : {status_str}")
    click.echo(f"Steps   : {result.steps_taken}")
    click.echo(f"Tokens  : {result.total_tokens:,}")
    click.echo(f"Time    : {elapsed:.1f}s")
    if result.error:
        click.echo(red(f"Error   : {result.error}"))
    click.echo(bold("─" * 60) + "\n")

    sys.exit(0 if result.is_success() else 1)



# ---------------------------------------------------------------------------
# chat 子命令 — 交互对话模式
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--mode", default="react", show_default=True, type=click.Choice(["react", "plan", "dag", "multi-agent", "auto"]), help="Agent mode")
@click.option("--max-steps", default=None, type=int, help="Max steps per round")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def chat(
    ctx: click.Context,
    repo: str,
    model: str | None,
    provider: str | None,
    mode: str,
    max_steps: int | None,
    sandbox: bool,
    verbose: bool,
) -> None:
    """Interactive chat mode — continuous conversation with the agent."""
    import logging
    from entry.chat import ChatSession

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(config, provider=provider, model=model, max_steps=max_steps, max_tokens=None)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    try:
        backend = create_backend_from_config({
            "provider":   config.llm.provider,
            "model":      config.llm.model,
            "api_key":    config.llm.api_key or None,
            "base_url":   config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    # 初始化记忆系统
    memory_store = None
    memory_context = None
    external_store = None
    if config.memory.enabled:
        from memory.store import TwoTierMemoryStore
        from memory.context import MemoryContext
        from memory.external_store import ExternalMemoryStore
        from memory.indexer import MemoryIndexer
        from memory.retriever import ProactiveRetriever

        external_store = ExternalMemoryStore()
        indexer = MemoryIndexer(external_store)
        memory_store = TwoTierMemoryStore(
            repo_path=str(repo_path),
            memory_dir=config.memory.directory or None,
            max_index_lines=config.memory.max_index_lines,
            indexer=indexer,
        )
        retriever = ProactiveRetriever(external_store, max_chunks=5, max_tokens=2000)
        memory_context = MemoryContext(
            store=memory_store,
            max_lines=config.memory.max_index_lines,
            enabled=config.memory.enabled,
            retriever=retriever,
        )

    registry = _build_registry(config, memory_store=memory_store, external_store=external_store)
    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    from entry.renderer import create_renderer
    rend = create_renderer(model=config.llm.model, mode="react")
    session = ChatSession(
        backend=backend,
        registry=registry,
        config=config,
        repo_path=str(repo_path),
        log_dir=config.agent.log_dir,
        confirm_callback=terminal_confirm,
        renderer=rend,
        memory_store=memory_store,
        memory_context=memory_context,
    )

    # 设置初始模式
    if mode != "react":
        session.switch_mode(mode)

    # 欢迎信息
    click.echo(bold(f"\nCoding Agent — Chat Mode"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Mode     : {mode}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(dim(f"  Type your task. Commands: /exit /stats /clear /help\n"))

    # 启用行编辑：退格、方向键、Ctrl+A/E、历史记录（↑↓）
    try:
        import readline as _rl
        import sys as _sys
        # 检测后端：libedit（某些 Linux/macOS）还是 GNU readline
        _is_libedit = "libedit" in getattr(_rl, "__doc__", "") or (
            hasattr(_rl, "parse_and_bind") and _sys.platform == "darwin"
        )
        # 更可靠的检测：尝试 libedit 特有的绑定语法
        try:
            _rl.parse_and_bind("bind -e")   # libedit 启用 Emacs 模式
            _is_libedit = True
        except Exception:
            _is_libedit = False

        if _is_libedit:
            _rl.parse_and_bind("bind -e")           # Emacs 模式：Ctrl+A/E/K 等
            _rl.parse_and_bind("bind ^I rl_complete")  # Tab 补全
        else:
            _rl.parse_and_bind("set editing-mode emacs")  # GNU readline Emacs 模式
            _rl.parse_and_bind("tab: complete")

        _rl.set_history_length(500)   # 历史记录最多 500 条
    except ImportError:
        pass  # Windows 没有 readline，降级为普通 input

    # 主 REPL 循环
    while True:
        try:
            # 清理当前行（流式输出后 readline 不知道屏幕上有残留字符）
            # \r 回到行首，\033[2K 清除整行，然后显示提示符
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            user_input = input(magenta("you") + " > ").strip()
        except EOFError:
            click.echo()
            break
        except KeyboardInterrupt:
            click.echo()
            break

        if not user_input:
            continue

        # 内置命令
        if user_input.startswith("/"):
            cmd = user_input.lower()
            if cmd in ("/exit", "/quit", "/q"):
                break
            elif cmd == "/stats":
                session.print_stats()
            elif cmd == "/clear":
                session._shared_history.clear_except_first()
                click.echo(dim("  History cleared (kept initial context)."))
            elif cmd == "/compact":
                msg = session.compact()
                click.echo(dim(f"  {msg}"))
            elif cmd.startswith("/mode"):
                parts = user_input.strip().split()
                if len(parts) == 2 and parts[1] in ("react", "plan", "dag", "multi-agent", "auto"):
                    session.switch_mode(parts[1])
                    click.echo(dim(f"  Mode switched to: {parts[1]}"))
                else:
                    current = getattr(session, "_mode", "react")
                    click.echo(dim(
                        f"  Current mode: {current}\n"
                        f"  Usage: /mode react|plan|dag|multi-agent|auto"
                    ))
            elif cmd.startswith("/model"):
                parts = user_input.strip().split(maxsplit=2)
                if len(parts) >= 2:
                    session.switch_model(parts[1])
                    click.echo(dim(f"  Model switched to: {parts[1]}"))
                else:
                    m = getattr(session, "_model", "?")
                    p = getattr(session, "_provider", "?")
                    click.echo(dim(f"  Current: {m} (provider: {p})\n  Usage: /model <model-name>"))
            elif cmd == "/help":
                click.echo(dim(
                    "  Commands:\n"
                    "    /exit    — quit\n"
                    "    /stats   — show session statistics\n"
                    "    /clear   — clear conversation history\n"
                    "    /compact — compress conversation to save context\n"
                    "    /mode    — show or switch agent mode (react|plan|dag|multi-agent|auto)\n"
                    "    /model   — show or switch LLM model\n"
                    "    /help    — show this help\n"
                    "  Anything else is sent to the agent."
                ))
            else:
                click.echo(dim(f"  Unknown command: {user_input}. Type /help for help."))
            continue

        # 运行一轮 agent
        click.echo(dim(f"\n  Agent working..."))
        try:
            session.run_round(user_input)
        except KeyboardInterrupt:
            click.echo(yellow("\n  Interrupted. Type /exit to quit or continue with a new task."))
        except Exception as e:
            click.echo(red(f"\n  Error: {e}"))
            if verbose:
                import traceback
                traceback.print_exc()

    session.print_stats()
    click.echo(dim("  Bye!\n"))


# ---------------------------------------------------------------------------
# log 子命令组
# ---------------------------------------------------------------------------

@cli.group()
def log() -> None:
    """Inspect event logs."""


@log.command("show")
@click.argument("log_file")
def log_show(log_file: str) -> None:
    """Show a summary of an event log file."""
    from agent.event_log import EventLog, summarize_run

    path = Path(log_file)
    if not path.exists():
        click.echo(red(f"File not found: {path}"), err=True)
        sys.exit(1)

    with EventLog.open_existing(path) as elog:
        events = elog.replay()
        stats = summarize_run(elog)

    click.echo(bold(f"\nEvent Log: {path.name}"))
    click.echo(f"  Total events : {stats['total_events']}")
    click.echo(f"  Actions      : {stats['actions']}")
    click.echo(f"  Reflections  : {stats['reflections']}")
    click.echo(f"  Tool calls   : {stats['tool_calls']}")
    click.echo(f"  Final status : {stats['final_status']}\n")

    click.echo(bold("Events:"))
    for event in events:
        ts = event.timestamp[11:19]   # HH:MM:SS
        etype = event.event_type.value
        detail = ""
        if event.event_type.value == "action":
            tcs = event.payload.get("action", {}).get("tool_calls") or []
            detail = f"  tools={[tc['name'] for tc in tcs]}" if tcs else ""
        elif event.event_type.value == "observation":
            obs = event.payload.get("observation", {})
            detail = f"  status={obs.get('status')}"
        click.echo(f"  {ts}  {etype:<16}{detail}")


@log.command("list")
@click.option("--dir", "log_dir", default="./logs", help="Log directory")
def log_list(log_dir: str) -> None:
    """List all event log files."""
    log_path = Path(log_dir)
    if not log_path.exists():
        click.echo(f"Log directory not found: {log_path}")
        return

    files = sorted(log_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No log files found.")
        return

    click.echo(bold(f"\nLog files in {log_path}:\n"))
    for f in files:
        size_kb = f.stat().st_size / 1024
        click.echo(f"  {f.name}  ({size_kb:.1f} KB)")
    click.echo()


# ---------------------------------------------------------------------------
# history 子命令组 — 对话历史可视化
# ---------------------------------------------------------------------------

@cli.group()
def history() -> None:
    """View and search conversation history (~/.forge-agent/history/)."""


@history.command("list")
@click.option("--limit", "-n", default=20, help="Max entries to show")
def history_list(limit: int) -> None:
    """List recent conversation sessions."""
    from entry.history_viewer import list_history

    sessions = list_history(limit=limit)
    if not sessions:
        click.echo(dim("  No history found. Run some chat sessions first."))
        return

    click.echo(bold(f"\n  Recent Sessions ({len(sessions)}):\n"))
    for s in sessions:
        ts = s["timestamp"][:19] if s["timestamp"] else "?"
        status_icon = green("✓") if s["status"] == "success" else (
            red("✗") if s["status"] == "failed" else dim("?")
        )
        task = s["task"][:50] if s["task"] else "(no description)"
        click.echo(f"  {status_icon} {dim(ts)}  {task}")
        click.echo(dim(f"      {s['steps']} steps · {s['file']}"))
    click.echo()


@history.command("show")
@click.argument("session_file")
def history_show(session_file: str) -> None:
    """Show detailed view of a session log file."""
    from entry.history_viewer import render_history_detail, get_history_dir

    path = Path(session_file)
    if not path.exists():
        # try in history dir
        path = get_history_dir() / session_file
    if not path.exists():
        click.echo(red(f"  File not found: {session_file}"), err=True)
        sys.exit(1)

    output = render_history_detail(path)
    click.echo(output)


@history.command("search")
@click.argument("query")
@click.option("--limit", "-n", default=10, help="Max results")
def history_search(query: str, limit: int) -> None:
    """Search history for sessions containing a query string."""
    from entry.history_viewer import search_history

    results = search_history(query, limit=limit)
    if not results:
        click.echo(dim(f"  No sessions found matching: {query}"))
        return

    click.echo(bold(f"\n  Search results for '{query}' ({len(results)}):\n"))
    for s in results:
        ts = s["timestamp"][:19] if s["timestamp"] else "?"
        task = s["task"][:50] if s["task"] else "(no description)"
        click.echo(f"  {dim(ts)}  {task}")
        click.echo(dim(f"      {s['file']}"))
    click.echo()


@history.command("archive")
@click.option("--dir", "log_dir", default="./logs", help="Log directory to archive from")
def history_archive(log_dir: str) -> None:
    """Archive all log files from the logs directory to ~/.forge-agent/history/."""
    from entry.history_viewer import archive_log

    log_path = Path(log_dir)
    if not log_path.exists():
        click.echo(red(f"  Log directory not found: {log_path}"), err=True)
        return

    files = list(log_path.glob("*.jsonl"))
    if not files:
        click.echo(dim("  No log files to archive."))
        return

    archived = 0
    for f in files:
        result = archive_log(f)
        if result:
            archived += 1

    click.echo(green(f"  Archived {archived} session(s) to ~/.forge-agent/history/"))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()