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
import json
import logging
import os
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

# Windows 终端强制 UTF-8 输出（避免 GBK 编码错误）
if sys.platform == "win32":
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
from observability import configure_observability, flush_observability  # noqa: E402
from agent.prompt import reset_prompt_usage, set_project_dir, set_prompt_config  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助：彩色输出
# ---------------------------------------------------------------------------

from entry._terminal import bold, cyan, dim, green, magenta, red, yellow


# ---------------------------------------------------------------------------
# 初始化记忆系统
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)
from entry.bootstrap import init_memory as _init_memory
from entry.bootstrap import init_hook_dispatcher as _init_hook_dispatcher
from entry.bootstrap import build_registry as _build_registry
from entry.modes.v2_runner import _render_v2_event, _print_v2_result, run_v2_mode as _run_v2_mode


def _print_step(event) -> None:
    """实时打印单条 event。"""
    from agent.task import EventType, ObservationStatus
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
        if status == ObservationStatus.SUCCESS.value:
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


from entry.worktree_admin import worktree_admin  # noqa: E402
cli.add_command(worktree_admin)


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
@click.option("--agent", "agent_name", default="build", show_default=True, type=click.Choice(["build", "plan"]), help="Agent: build (edit) or plan (analysis)")
@click.option("--auto-approve", is_flag=True, default=False, help="Auto-approve tool permission prompts; does not execute a generated plan")
@click.option(
    "--plan-action",
    type=click.Choice(["review", "save", "execute"]),
    default="review",
    show_default=True,
    help="After v2-plan succeeds: review interactively, save only, or execute",
)
@click.option("--replan", is_flag=True, default=False, help="Enable one or more DAG replans after subtask failure")
@click.option("--max-replans", default=None, type=int, help="Maximum DAG replan attempts")
@click.option("--read", "read_paths", multiple=True, default=None, help="Explicitly allowed read path (repeatable)")
@click.option("--write", "write_paths", multiple=True, default=None, help="Explicitly allowed write path (repeatable)")
@click.option("--intent", "intent_override", default=None, type=click.Choice(["analysis", "edit"]), help="Override the task intent declared by the selected mode")
@click.option("--plan-file", default=None, help="Inject an approved plan file into v2-build session")
@click.option(
    "--delegate-to",
    default=None,
    metavar="AGENT",
    help="Guarantee one named subagent runs before the primary agent synthesizes the result",
)
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
    agent_name: str,
    auto_approve: bool,
    plan_action: str,
    replan: bool,
    max_replans: int | None,
    read_paths: tuple[str, ...] | None,
    write_paths: tuple[str, ...] | None,
    intent_override: str | None,
    plan_file: str | None,
    delegate_to: str | None,
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
    configure_observability(config)
    set_prompt_config(config.prompts)

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
    set_project_dir(str(repo_path))
    reset_prompt_usage()

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
            "timeout_seconds": config.llm.timeout_seconds,
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
        memory_store, memory_context, external_store = _init_memory(str(repo_path), config)

    from hitl.pipeline import ToolApprovalMode
    registry = _build_registry(
        config,
        confirm_callback=confirm_cb,
        runtime=runtime,
        memory_store=memory_store,
        external_store=external_store,
        repo_path=repo_path,
        approval_mode=(
            ToolApprovalMode.AUTO if auto_approve else ToolApprovalMode.PROMPT
        ),
    )

    # Initialize HookDispatcher
    hook_dispatcher = _init_hook_dispatcher(
        repo_path,
        memory_store=memory_store,
        log_dir=config.agent.log_dir,
        backend=backend,
    )

    # Wire hook_dispatcher into ToolRegistry (PostToolUse) and PermissionPipeline (PreToolUse)
    registry._hook_dispatcher = hook_dispatcher
    if hasattr(registry, '_permission_pipeline') and registry._permission_pipeline is not None:
        registry._permission_pipeline._hook_dispatcher = hook_dispatcher

    # memory 模块日志可见性
    if config.memory.enabled:
        logging.getLogger("memory").setLevel(logging.INFO)

    from agent.core import AgentConfig
    from agent.event_log import EventLog, summarize_run
    from agent.task import Task
    from agent.policy import normalize_repo_path
    from entry.renderer import create_renderer
    try:
        from context.token_budget import is_tiktoken_available
    except ImportError:
        is_tiktoken_available = lambda: False

    # 创建渲染器
    rend = create_renderer(model=config.llm.model, mode=agent_name)

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        request_budget_tokens=config.context.request_budget_tokens,
        artifact_threshold_tokens=config.context.artifact_threshold_tokens,
        artifact_storage_dir=config.context.artifact_storage_dir,
        history_max_messages=config.context.history_window * 2,
        stream=stream,
        stream_callback=rend.stream_text if stream else None,
        thought_callback=rend.stream_thought if stream else None,
        token_callback=rend.update_tokens,
        confirm_dangerous=confirm,
        confirm_callback=confirm_cb,
    )
    mcp_integration = None
    if agent_name in ("build", "plan") and getattr(config, "mcp_servers", None):
        from agent.v2 import MCPToolIntegration
        mcp_integration = MCPToolIntegration({"mcp_servers": config.mcp_servers})
        mcp_integration.initialize()
        mcp_integration.register_into(registry)

    if agent_name in ("build", "plan"):
        from agent.v2 import AgentDefinitionError, ExplicitDelegationError
        try:
            from entry.modes.interaction import cli_plan_adapter
            mode_result = _run_v2_mode(
                agent_name=agent_name,
                description=description,
                repo_path=repo_path,
                backend=backend,
                registry=registry,
                agent_config=agent_config,
                memory_context=memory_context,
                log_dir=config.agent.log_dir,
                intent_override=intent_override,
                approval_interaction=cli_plan_adapter(plan_action),
                plan_file=plan_file,
                hook_dispatcher=hook_dispatcher,
                mcp_integration=mcp_integration,
                renderer=rend,
                explicit_agent=delegate_to,
            )
        except (AgentDefinitionError, ExplicitDelegationError) as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            if mcp_integration is not None:
                mcp_integration.shutdown()
        flush_observability()
        if not mode_result.is_success():
            raise click.exceptions.Exit(1)
        return

    click.echo(red(f"Error: unknown agent '{agent_name}'. Use --agent build or --agent plan."), err=True)
    sys.exit(1)



# ---------------------------------------------------------------------------
# chat 子命令 — 交互对话模式
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--agent", "agent_name", default="build", show_default=True, type=click.Choice(["build", "plan"]), help="Agent: build (edit) or plan (analysis)")
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
    configure_observability(config)
    set_prompt_config(config.prompts)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)
    set_project_dir(str(repo_path))

    try:
        backend = create_backend_from_config({
            "provider":   config.llm.provider,
            "model":      config.llm.model,
            "api_key":    config.llm.api_key or None,
            "base_url":   config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
            "timeout_seconds": config.llm.timeout_seconds,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    # 初始化记忆系统
    memory_store = None
    memory_context = None
    external_store = None
    if config.memory.enabled:
        memory_store, memory_context, external_store = _init_memory(str(repo_path), config)

    # Skill 系统初始化
    from skills.registry import SkillRegistry
    skills_dir = os.path.join(str(repo_path), ".forge-agent", "skills")
    skill_registry = SkillRegistry(skills_dir)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None

    registry = _build_registry(
        config,
        confirm_callback=terminal_confirm,
        runtime=runtime,
        memory_store=memory_store,
        external_store=external_store,
        repo_path=repo_path,
    )

    # 注册 SkillTool（如果有已发现的 skills）
    if skill_registry.list_skills():
        from skills.tool import SkillTool
        registry.register(SkillTool(skill_registry))
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
        skill_registry=skill_registry,
    )

    # 设置初始模式
    if mode != session._mode:
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
                focus = user_input[len("/compact"):].strip()
                msg = session.compact(focus=focus)
                click.echo(dim(f"  {msg}"))
            elif cmd.startswith("/mode"):
                parts = user_input.strip().split()
                click.echo(dim("  Agent modes are set at startup via --mode. Runtime switching has been removed."))
            elif cmd.startswith("/model"):
                parts = user_input.strip().split(maxsplit=2)
                if len(parts) >= 2:
                    session.switch_model(parts[1])
                    click.echo(dim(f"  Model switched to: {parts[1]}"))
                else:
                    m = getattr(session, "_model", "?")
                    p = getattr(session, "_provider", "?")
                    click.echo(dim(f"  Current: {m} (provider: {p})\n  Usage: /model <model-name>"))
            elif cmd.startswith("/skill"):
                parts = user_input.strip().split(maxsplit=2)
                subcmd = parts[1] if len(parts) > 1 else "list"
                if subcmd == "list":
                    skills = skill_registry.list_skills()
                    if not skills:
                        click.echo(dim("  No skills available."))
                    else:
                        click.echo(dim(f"  Available skills ({len(skills)}):"))
                        for s in skills:
                            click.echo(dim(f"    /{s.name:<14} — {s.description or '(no description)'}"))
                elif subcmd == "show":
                    skill_name = parts[2] if len(parts) > 2 else ""
                    if not skill_name:
                        click.echo(dim("  Usage: /skill show <name>"))
                    else:
                        body = skill_registry.get_skill_detail(skill_name)
                        if body:
                            click.echo(f"\n{body}\n")
                        else:
                            click.echo(dim(f"  Skill '{skill_name}' not found."))
                elif subcmd == "reload":
                    skill_registry.refresh()
                    click.echo(dim(f"  Reloaded. {len(skill_registry.list_skills())} skills discovered."))
                else:
                    click.echo(dim("  Usage: /skill list | /skill show <name> | /skill reload"))
            elif cmd.startswith("/goal"):
                from runtime.goal import GoalState, MAX_GOAL_CONDITION_CHARS
                args = user_input[len("/goal"):].strip()
                clear_words = {"clear", "stop", "off", "reset", "none", "cancel"}
                if not args:
                    goal = session.goal_store.get()
                    if not goal or not goal.active:
                        click.echo(dim("  当前没有活跃的 /goal 目标。"))
                    else:
                        click.echo(dim(
                            f"  🎯 当前目标：{goal.condition}\n"
                            f"     轮数：{goal.turn_count}/{goal.max_turns}\n"
                            f"     状态：{'活跃' if goal.active else '已完成'}\n"
                            f"     上次评估：{goal.last_judge_reason or '尚未评估'}"
                        ))
                elif args.lower() in clear_words:
                    session.goal_store.clear()
                    click.echo(dim("  🎯 目标已清除。"))
                elif len(args) > MAX_GOAL_CONDITION_CHARS:
                    click.echo(red(
                        f"  条件过长（{len(args)} 字符），上限 {MAX_GOAL_CONDITION_CHARS} 字符。"
                    ))
                else:
                    session.goal_store.set(GoalState(
                        condition=args,
                        session_id=getattr(session, "_session_id", ""),
                    ))
                    click.echo(dim(f"  🎯 目标已设定：{args}\n  每轮结束后会自动评估。"))
            elif cmd.startswith("/mcp"):
                parts = user_input.strip().split(maxsplit=2)
                subcmd = parts[1] if len(parts) > 1 else ""
                if hasattr(registry, '_mcp_manager') and registry._mcp_manager:
                    mgr = registry._mcp_manager
                    if not subcmd:
                        click.echo(dim(f"  MCP servers: {len(mgr._configs)} configured, {len(mgr.proxies)} tools loaded"))
                        for cfg in mgr._configs:
                            tool_count = sum(1 for p in mgr.proxies if getattr(p, '_server_name', None) == cfg.name)
                            click.echo(dim(f"    {cfg.name}: {tool_count} tools"))
                        resources_count = sum(len(v) for v in getattr(mgr, '_resources', {}).values())
                        templates_count = sum(len(v) for v in getattr(mgr, '_resource_templates', {}).values())
                        if resources_count or templates_count:
                            click.echo(dim(f"  Resources: {resources_count} static, {templates_count} templates"))
                    elif subcmd == "resources":
                        resources = getattr(mgr, '_resources', {})
                        templates = getattr(mgr, '_resource_templates', {})
                        has_any = any(resources.values()) or any(templates.values())
                        if not has_any:
                            click.echo(dim("  No resources available."))
                        else:
                            for server, res_list in resources.items():
                                if res_list:
                                    click.echo(dim(f"  [{server}] Resources:"))
                                    for r in res_list:
                                        uri = getattr(r, 'uri', str(r))
                                        name = getattr(r, 'name', '')
                                        click.echo(dim(f"    {uri}  {name}"))
                            for server, tpl_list in templates.items():
                                if tpl_list:
                                    click.echo(dim(f"  [{server}] Templates:"))
                                    for t in tpl_list:
                                        uri_tpl = getattr(t, 'uriTemplate', str(t))
                                        name = getattr(t, 'name', '')
                                        click.echo(dim(f"    {uri_tpl}  {name}"))
                    elif subcmd == "prompts":
                        prompts = getattr(mgr, '_prompts', {})
                        if not prompts:
                            click.echo(dim("  No prompts available."))
                        else:
                            for server, prompt_list in prompts.items():
                                click.echo(dim(f"  [{server}]"))
                                for p in prompt_list:
                                    pname = getattr(p, 'name', str(p))
                                    pdesc = getattr(p, 'description', '')
                                    click.echo(dim(f"    {pname}: {pdesc}"))
                    else:
                        click.echo(dim("  Usage: /mcp | /mcp resources | /mcp prompts"))
                else:
                    click.echo(dim("  No MCP servers configured."))
            elif cmd == "/help":
                help_lines = [
                    "  Commands:",
                    "    /exit    — quit",
                    "    /stats   — show session statistics",
                    "    /clear   — clear conversation history",
                    "    /compact [focus] — compress conversation (optional: prioritize retaining focus topic)",
                    "    /goal [condition|clear] — set/show/clear a session completion goal",
                    "    /mode    — show or switch agent mode (react|plan|dag|multi-agent|auto)",
                    "    /model   — show or switch LLM model",
                    "    /skill   — list/show/reload skills",
                    "    /mcp     — show MCP server status",
                    "    /help    — show this help",
                ]
                # 列出可用 skills
                skills = skill_registry.list_skills()
                if skills:
                    help_lines.append("  Skills (type /name to activate):")
                    for s in skills:
                        help_lines.append(f"    /{s.name:<14} — {s.description or '(no description)'}")
                help_lines.append("  Anything else is sent to the agent.")
                click.echo(dim("\n".join(help_lines)))
            else:
                # Try /skill-name dispatch (Claude Code alignment: direct injection)
                skill_name = user_input[1:].split()[0]
                if skill_registry.has_skill(skill_name):
                    rendered = session._handle_slash_skill(user_input)
                    if rendered is not None:
                        # Inline skill — inject and run agent
                        click.echo(dim(f"\n  Skill '{skill_name}' activated..."))
                        session.run_round(rendered)
                    # else: context=fork — skill handled internally by _handle_slash_skill
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


@cli.command("langfuse-validate")
@click.option("--repo", "-r", default=".", show_default=True, help="Repository path used for the validation task")
@click.option(
    "--scenario",
    default="both",
    show_default=True,
    type=click.Choice(["success-readonly", "failure-low-budget", "both"]),
    help="Validation scenario to run",
)
@click.option("--json-out", default=None, help="Optional path to write structured validation results as JSON")
@click.option("--baseline-name", default=None, help="Optional baseline snapshot name to persist for later comparison")
@click.option("--baseline-out", default=None, help="Optional path to write the baseline snapshot JSON")
@click.pass_context
def langfuse_validate(
    ctx: click.Context,
    repo: str,
    scenario: str,
    json_out: str | None,
    baseline_name: str | None,
    baseline_out: str | None,
) -> None:
    """Run repeatable Langfuse end-to-end validation scenarios."""
    from agent.core import AgentConfig
    from agent.event_log import EventLog
    from agent.factory import create_agent
    from agent.task import Task
    from langfuse import get_client
    from observability.validation import (
        build_baseline_snapshot,
        default_baseline_output_path,
        ValidationResult,
        evaluate_validation_result,
        failure_dataset_line_count,
        load_validation_config,
        selected_validation_scenarios,
        write_baseline_snapshot,
        write_validation_results,
    )

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    config = load_validation_config(ctx.obj.get("config_path"))
    set_prompt_config(config.prompts)
    set_project_dir(str(repo_path))

    click.echo(bold("\nLangfuse Validation"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Scenario : {scenario}\n")

    backend = create_backend_from_config({
        "provider": config.llm.provider,
        "model": config.llm.model,
        "api_key": config.llm.api_key or None,
        "base_url": config.llm.base_url or None,
        "max_tokens": config.llm.max_tokens,
    })
    registry = _build_registry(config)
    observer = configure_observability(config)
    original_start_task = observer.start_task

    results: list[ValidationResult] = []
    try:
        for scenario_cfg in selected_validation_scenarios(scenario):
            reset_prompt_usage()
            trace_meta: dict[str, str] = {}

            def _wrapped_start_task(task):
                cm = original_start_task(task)

                class _Wrapper:
                    def __enter__(self):
                        handle = cm.__enter__()
                        try:
                            client = get_client()
                            trace_id = client.get_current_trace_id()
                            trace_meta["trace_id"] = str(trace_id) if trace_id else ""
                            trace_meta["trace_url"] = str(client.get_trace_url(trace_id=trace_id)) if trace_id else ""
                        except Exception as exc:
                            trace_meta["trace_capture_error"] = str(exc)
                        return handle

                    def __exit__(self, exc_type, exc, tb):
                        return cm.__exit__(exc_type, exc, tb)

                return _Wrapper()

            observer.start_task = _wrapped_start_task

            dataset_path, dataset_lines_before = failure_dataset_line_count(str(repo_path))
            agent = create_agent(
                scenario_cfg.mode,
                backend,
                registry,
                AgentConfig(
                    max_steps=scenario_cfg.max_steps,
                    budget_tokens=scenario_cfg.budget_tokens,
                    history_max_messages=20,
                    stream=False,
                ),
                task_description=scenario_cfg.description,
            )
            task = Task(
                description=scenario_cfg.description,
                repo_path=str(repo_path),
                intent=scenario_cfg.intent,
                max_steps=scenario_cfg.max_steps,
                budget_tokens=scenario_cfg.budget_tokens,
                metadata={
                    "entrypoint": "cli_langfuse_validate",
                    "mode": scenario_cfg.mode,
                    "session_id": f"langfuse-validate-{scenario_cfg.name}",
                    "provider": config.llm.provider,
                    "model": config.llm.model,
                    "validation_scenario": scenario_cfg.name,
                },
            )

            click.echo(cyan(f"[Scenario] {scenario_cfg.name}"))
            with EventLog.create(task, log_dir=config.agent.log_dir) as log:
                result = agent.run(task, log)
                log_path = str(log.path)
            flush_observability()

            _dataset_path, dataset_lines_after = failure_dataset_line_count(str(repo_path))
            dataset_new_entries = dataset_lines_after - dataset_lines_before
            passed, checks = evaluate_validation_result(
                scenario_cfg,
                actual_status=result.status.value,
                trace_id=trace_meta.get("trace_id"),
                dataset_new_entries=dataset_new_entries,
            )
            validation_result = ValidationResult(
                scenario=scenario_cfg.name,
                expected_status=scenario_cfg.expected_status,
                actual_status=result.status.value,
                passed=passed,
                repo_path=str(repo_path),
                summary=result.summary,
                steps=result.steps_taken,
                tokens=result.total_tokens,
                log_path=log_path,
                trace_id=trace_meta.get("trace_id") or None,
                trace_url=trace_meta.get("trace_url") or None,
                dataset_path=str(dataset_path),
                dataset_lines_before=dataset_lines_before,
                dataset_lines_after=dataset_lines_after,
                dataset_new_entries=dataset_new_entries,
                details=checks | ({k: v for k, v in trace_meta.items() if k not in {"trace_id", "trace_url"}}),
            )
            results.append(validation_result)

            status_text = green("PASS") if passed else red("FAIL")
            click.echo(f"  Result   : {status_text}")
            click.echo(f"  Status   : {result.status.value}")
            click.echo(f"  Trace    : {validation_result.trace_url or '(missing)'}")
            click.echo(f"  Log      : {log_path}")
            click.echo(f"  Dataset  : +{dataset_new_entries} -> {dataset_lines_after}\n")
    finally:
        observer.start_task = original_start_task

    if json_out:
        output_path = write_validation_results(results, json_out)
        click.echo(dim(f"  JSON report written to: {output_path}"))

    if baseline_name:
        baseline_snapshot = build_baseline_snapshot(
            baseline_name=baseline_name,
            repo_path=str(repo_path),
            provider=config.llm.provider,
            model=config.llm.model,
            prompt_source=config.prompts.source,
            prompt_label=config.prompts.label,
            prompt_version=config.prompts.version,
            results=results,
            metadata={
                "scenario_selection": scenario,
                "observability_environment": config.observability.environment,
            },
        )
        baseline_path = write_baseline_snapshot(
            baseline_snapshot,
            baseline_out or default_baseline_output_path(str(repo_path), baseline_name),
        )
        click.echo(dim(f"  Baseline snapshot written to: {baseline_path}"))

    if not all(result.passed for result in results):
        sys.exit(1)


# ---------------------------------------------------------------------------
# log 子命令组
# ---------------------------------------------------------------------------

@cli.group()
def log() -> None:
    """Inspect event logs."""


@log.command("filters")
@click.argument("log_files", nargs=-1)
@click.option(
    "--dir",
    "log_dir",
    default="",
    show_default=True,
    help="Load logs from a directory; empty uses isolated state for the current project",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON output")
def log_filters(log_files: tuple[str, ...], log_dir: str, as_json: bool) -> None:
    """Inspect session and subtask filter metadata from one or more event logs."""
    from observability.filtering import collect_log_filter_records, summarize_filter_groups

    if log_files:
        paths = [Path(log_file) for log_file in log_files]
    else:
        if log_dir:
            log_path = Path(log_dir)
        else:
            from runtime.state_paths import ProjectStatePaths
            log_path = ProjectStatePaths.for_project(Path.cwd()).logs
        if not log_path.exists():
            click.echo(red(f"Log directory not found: {log_path}"), err=True)
            sys.exit(1)
        paths = sorted(log_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    missing = [path for path in paths if not path.exists()]
    if missing:
        click.echo(red(f"File not found: {missing[0]}"), err=True)
        sys.exit(1)

    if not paths:
        click.echo("No log files found.")
        return

    records = collect_log_filter_records(paths)
    groups = summarize_filter_groups(records)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "records": [record.to_dict() for record in records],
                    "groups": groups,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    click.echo(bold(f"\nFilter Records ({len(records)}):"))
    for record in records:
        parts = [
            f"file={Path(record.log_path).name}",
            f"task_id={record.task_id or '-'}",
            f"mode={record.mode or '-'}",
            f"session_id={record.session_id or '-'}",
            f"round={record.round if record.round is not None else '-'}",
            f"parent_task_id={record.parent_task_id or '-'}",
            f"subtask_id={record.subtask_id or '-'}",
            f"role={record.role or '-'}",
            f"agent_id={record.agent_id or '-'}",
            f"final={record.final_event or '-'}",
        ]
        click.echo(f"  {' | '.join(parts)}")

    click.echo(bold("\nFilter Groups:"))
    for group_name, counts in groups.items():
        if not counts:
            click.echo(f"  {group_name}: (none)")
            continue
        summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        click.echo(f"  {group_name}: {summary}")
    click.echo()


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
        if event.event_type is EventType.ACTION:
            tcs = event.payload.get("action", {}).get("tool_calls") or []
            detail = f"  tools={[tc['name'] for tc in tcs]}" if tcs else ""
        elif event.event_type is EventType.OBSERVATION:
            obs = event.payload.get("observation", {})
            detail = f"  status={obs.get('status')}"
        click.echo(f"  {ts}  {etype:<16}{detail}")


@log.command("list")
@click.option("--dir", "log_dir", default="", help="Log directory; empty uses isolated project state")
def log_list(log_dir: str) -> None:
    """List all event log files."""
    if log_dir:
        log_path = Path(log_dir)
    else:
        from runtime.state_paths import ProjectStatePaths
        log_path = ProjectStatePaths.for_project(Path.cwd()).logs
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
@click.option("--dir", "log_dir", default="", help="Log directory; empty uses isolated project state")
def history_archive(log_dir: str) -> None:
    """Archive all log files from the logs directory to ~/.forge-agent/history/."""
    from entry.history_viewer import archive_log

    if log_dir:
        log_path = Path(log_dir)
    else:
        from runtime.state_paths import ProjectStatePaths
        log_path = ProjectStatePaths.for_project(Path.cwd()).logs
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
