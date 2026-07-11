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
from observability import configure_observability, flush_observability  # noqa: E402
from agent.prompt import reset_prompt_usage, set_project_dir, set_prompt_config  # noqa: E402


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
# 初始化记忆系统
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _init_memory(repo_path: str, config) -> tuple:
    """
    初始化记忆系统，返回 (memory_store, memory_context, external_store)。
    fastembed 不可用时优雅降级：禁用语义搜索，仅保留文件索引。
    """
    from memory.store import TwoTierMemoryStore
    from memory.context import MemoryContext
    from llm.router import create_selector_backend

    retriever = None
    external_store = None
    indexer = None

    try:
        import fastembed as _  # noqa: F401
        from memory.external_store import ExternalMemoryStore
        from memory.indexer import MemoryIndexer
        from memory.retriever import ProactiveRetriever

        external_store = ExternalMemoryStore()
        indexer = MemoryIndexer(external_store)
        retriever = ProactiveRetriever(external_store, max_chunks=5, max_tokens=2000)
    except ImportError:
        logger.info(
            "fastembed not installed — semantic memory search disabled. "
            "Install: pip install 'coding-agent[rag]'"
        )

    memory_store = TwoTierMemoryStore(
        repo_path=repo_path,
        memory_dir=config.memory.directory or None,
        max_index_lines=config.memory.max_index_lines,
        indexer=indexer,
    )
    selector_backend = create_selector_backend({
        "memory": {
            "selector_enabled": config.memory.selector_enabled,
            "selector_model": config.memory.selector_model,
        },
        "llm": {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "api_key": config.llm.api_key,
            "base_url": config.llm.base_url,
        },
    })
    memory_context = MemoryContext(
        store=memory_store,
        max_lines=config.memory.max_index_lines,
        enabled=config.memory.enabled,
        retriever=retriever,
        selector_backend=selector_backend,
    )
    return memory_store, memory_context, external_store


# ---------------------------------------------------------------------------
# Hook Dispatcher 初始化
# ---------------------------------------------------------------------------

def _init_hook_dispatcher(repo_path: Path, proactive_memory=None, memory_store=None,
                          log_dir: str | None = None, backend=None):
    """Create HookDispatcher with optional ProactiveMemory as internal hooks."""
    from hooks import HookDispatcher, HookEvent, HookMatcher, HookRegistry, InternalHook

    registry = HookRegistry()

    # Load external hooks from settings.json
    settings_path = repo_path / ".forge-agent" / "settings.json"
    registry.load_from_settings(settings_path)

    # Register ProactiveMemory as internal PostToolUse/UserPromptSubmit subscriber
    if proactive_memory is not None:
        registry.register_internal(HookEvent.POST_TOOL_USE, InternalHook(
            callback=lambda ctx: proactive_memory.check_tool_result(
                ctx.tool_name,
                ctx.tool_input,
                (ctx.tool_output or {}).get("output", ""),
                (ctx.tool_output or {}).get("success", False),
            ),
            matcher=HookMatcher(pattern="shell"),
        ))
        # Detect explicit memory_write → suppress auto-extraction this turn
        registry.register_internal(HookEvent.POST_TOOL_USE, InternalHook(
            callback=lambda ctx: proactive_memory.notify_explicit_memory_write(),
            matcher=HookMatcher(pattern="memory_write"),
        ))
        def _on_user_prompt(ctx):
            proactive_memory.reset_turn()
            proactive_memory.check_user_message(ctx.user_input)

        registry.register_internal(HookEvent.USER_PROMPT_SUBMIT, InternalHook(
            callback=_on_user_prompt,
        ))

    # Register memory consolidation on SessionStop
    if memory_store is not None:
        def _on_session_stop(ctx):
            from memory.consolidation import record_session_end, run_consolidation
            try:
                record_session_end(memory_store.store_dir)
                run_consolidation(memory_store, log_dir=log_dir, backend=backend, async_run=True)
            except Exception:
                pass

        registry.register_internal(HookEvent.STOP, InternalHook(
            callback=_on_session_stop,
        ))

    return HookDispatcher(registry, cwd=str(repo_path))


# ---------------------------------------------------------------------------
# 构建 agent 各组件
# ---------------------------------------------------------------------------

def _build_registry(cfg, confirm_callback=None, runtime=None, memory_store=None,
                    external_store=None, repo_path=None, auto_approve=False):
    """根据配置组装工具注册表。"""
    from tools.base import ToolRegistry
    from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool
    from tools.file_edit_tool import FileEditTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool
    from tools.web_tool import WebSearchTool, WebFetchTool
    from tools.artifact_tool import ArtifactListTool, ArtifactReadTool, ArtifactStoreRef
    from tools.evidence_tool import ArtifactSearchTool, EvidenceGetTool, EvidenceLedgerRef, EvidenceListTool
    from tools.submit_plan_tool import SubmitReadPlanRef, SubmitReadPlanTool

    # ── 构建 PermissionPipeline（5 层权限管道）──
    from hitl.pipeline import PermissionPipeline
    from hitl.settings_loader import load_permission_settings

    project_root = str(repo_path) if repo_path else None
    rules, _hook_configs = load_permission_settings(project_root or ".")

    perm_confirm = None
    if confirm_callback is not None:
        from entry.renderer import permission_prompt
        perm_confirm = permission_prompt

    settings_path = None
    if project_root:
        from pathlib import Path
        settings_path = str(Path(project_root) / ".forge-agent" / "settings.json")

    pipeline = PermissionPipeline(
        rules=rules,
        confirm_callback=perm_confirm,
        auto_approve=auto_approve,
        settings_path=settings_path,
        project_root=project_root,
    )

    web_cfg = cfg.tools.web
    artifact_store_ref = ArtifactStoreRef()
    evidence_ledger_ref = EvidenceLedgerRef()
    submit_plan_ref = SubmitReadPlanRef()

    # ── P1-1: Session-global FileReadCache shared across all agents ──
    from tools.file_tool import FileReadCache
    _global_read_cache = FileReadCache()

    registry = (
        ToolRegistry(permission_pipeline=pipeline)
        .register(ShellTool(runtime=runtime))
        .register(FileReadTool(read_cache=_global_read_cache))
        .register(FileViewTool(read_cache=_global_read_cache))
        .register(FileWriteTool(read_cache=_global_read_cache))
        .register(FileEditTool(read_cache=_global_read_cache))
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
        .register(ArtifactListTool(artifact_store_ref))
        .register(ArtifactReadTool(artifact_store_ref))
        .register(ArtifactSearchTool(artifact_store_ref))
        .register(EvidenceListTool(evidence_ledger_ref))
        .register(EvidenceGetTool(evidence_ledger_ref))
        .register(SubmitReadPlanTool(submit_plan_ref))
    )
    registry._artifact_store_ref = artifact_store_ref
    registry._evidence_ledger_ref = evidence_ledger_ref
    registry._submit_plan_ref = submit_plan_ref

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


def _render_v2_event(event, rend, proactive_memory=None, last_tool=None, last_tool_params=None):
    from agent.task import EventType

    payload = event.payload
    if event.event_type == EventType.ACTION:
        step = payload.get("step", 0)
        action = payload.get("action", {})
        tool_calls = action.get("tool_calls") or []
        if tool_calls:
            for tool_call in tool_calls:
                if last_tool is not None:
                    last_tool[0] = tool_call.get("name", "")
                if last_tool_params is not None:
                    last_tool_params[0] = tool_call.get("params", {})
                rend.on_tool_call(step, tool_call.get("name", ""), tool_call.get("params", {}))
        elif action.get("action_type") == "finish":
            rend.on_finish(step, action.get("message", ""))
        elif action.get("action_type") == "give_up":
            rend.on_give_up(step, action.get("message", ""))
    elif event.event_type == EventType.OBSERVATION:
        step = payload.get("step", 0)
        obs = payload.get("observation", {})
        tool_name = obs.get("tool_name") or (last_tool[0] if last_tool else "")
        output = obs.get("output", "")
        status = obs.get("status", "")
        rend.on_observation(step, tool_name, status, output, obs.get("error"))
        if proactive_memory is not None:
            proactive_memory.check_tool_result(
                tool_name=tool_name,
                params=(last_tool_params[0] if last_tool_params else {}),
                output=output,
                success=(status == "success"),
            )
    elif event.event_type == EventType.REFLECTION:
        rend.on_reflection(payload.get("reason", ""))


def _run_v2_mode(
    *,
    mode: str,
    description: str,
    repo_path: Path,
    backend,
    registry,
    agent_config,
    memory_context,
    log_dir: str,
    intent_override: str,
    plan_approval_callback=None,
    auto_approve: bool = False,
    plan_file: str | None = None,
    hook_dispatcher=None,
    proactive_memory=None,
    mcp_integration=None,
    renderer=None,
) -> None:
    import os
    import subprocess
    from datetime import datetime
    from agent.task import RunStatus
    from agent.factory import classify_task_intent
    from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore, default_session_db_path
    from llm.base import LLMMessage

    db_path = default_session_db_path(str(repo_path))
    store = SessionStore(db_path)
    rend = renderer
    last_tool = [""]
    last_tool_params = [{}]
    runtime = SessionRuntime(
        store=store,
        backend=backend,
        base_registry=registry,
        agent_registry=AgentRegistryV2(),
        root_agent_config=agent_config,
        log_dir=log_dir,
        memory_context=memory_context,
        hook_dispatcher=hook_dispatcher,
        mcp_integration=mcp_integration,
        event_callback=(
            (lambda event: _render_v2_event(
                event, rend, proactive_memory=proactive_memory,
                last_tool=last_tool, last_tool_params=last_tool_params,
            )) if rend is not None else None
        ),
    )
    intent = classify_task_intent(description, intent_override, backend)

    if mode == "v2-build":
        # ── 上下文连续：如果提供了 --plan-file，将计划文件内容注入 build session ──
        build_messages: list[LLMMessage] = []
        if plan_file and os.path.isfile(plan_file):
            with open(plan_file, encoding="utf-8") as f:
                plan_content = f.read()
            click.echo(dim(f"  Plan file: {plan_file}"))
            build_messages.append(LLMMessage(
                role="user",
                content=(
                    f"[PLAN CONTEXT] The following implementation plan has been reviewed and approved. "
                    f"Execute it now.\n\n{plan_content}"
                ),
            ))
        build_messages.append(LLMMessage(role="user", content=description))

        session = runtime.create_root_session(
            agent_name="build",
            repo_path=str(repo_path),
            title=description[:80] or "v2-build",
            metadata={"entrypoint": "cli_run_v2", "mode": mode},
        )
        result = runtime.run_session(
            session.id,
            agent_name="build",
            task_description=description,
            intent=intent,
            messages=build_messages,
        )
        _print_v2_result(mode, db_path, session.id, result, show_summary=False)
        return

    # --- plan / v2-plan: 权限过滤器 ---
    # plan agent = 只读工具（模型看到写工具但调用被拦截），用户审查后手动切 build。
    # 封闭循环：模型提交计划 → 用户审批 → 拒绝时反馈注入 → 模型重新规划 → 再审批
    # 计划文件 = single source of truth，原地覆盖（对齐 Claude Code .claude/plans/[name].md）
    if mode in ("plan", "v2-plan"):
        session = runtime.create_root_session(
            agent_name="plan",
            repo_path=str(repo_path),
            title=description[:80] or "plan",
            metadata={"entrypoint": "cli_run_v2", "mode": mode},
        )
        plan_steps = max(5, agent_config.max_steps // 3)
        plan_budget = max(5000, agent_config.budget_tokens // 3)

        # 固定计划文件路径（single file, 原地覆盖）
        plans_dir = os.path.join(str(repo_path), ".forge-agent", "plans")
        os.makedirs(plans_dir, exist_ok=True)
        task_slug = description[:40].replace(" ", "_").replace("/", "_").replace("\\", "_")
        plan_path = os.path.join(plans_dir, f"{task_slug}.md")

        # 首次 plan session
        result = runtime.run_session(
            session.id,
            agent_name="plan",
            task_description=description,
            intent="analysis",
            messages=[LLMMessage(role="user", content=description)],
            max_steps_override=plan_steps,
            budget_tokens_override=plan_budget,
        )

        # ── Plan 审批循环（封闭循环直到批准或用户退出）────
        max_revisions = 5
        revision_count = 0
        while True:
            plan_text = result.summary or ""

            # 原地覆盖同一个计划文件
            if plan_text.strip():
                with open(plan_path, "w", encoding="utf-8") as f:
                    f.write(plan_text)

            _print_v2_result(mode, db_path, session.id, result, show_summary=False)
            if plan_text.strip():
                click.echo(dim(f"  Plan    : {plan_path}"))

            # auto_approve → 跳过审核
            if auto_approve:
                if plan_text.strip():
                    click.echo(green("  Auto-approved."))
                return

            if not plan_text.strip():
                click.echo(yellow("  Plan session produced no output. Nothing to review."))
                return

            # ── 交互式审批菜单（对齐 Claude Code 5 选项）────
            click.echo("\n" + "─" * 60)
            click.echo(bold("  Plan ready for review"))
            click.echo(f"  File: {plan_path}")
            click.echo("─" * 60)
            click.echo(f"  [1] Yes, and auto-accept edits")
            click.echo(f"  [2] Yes, and manually approve edits")
            click.echo(f"  [3] Edit plan file (opens editor)")
            click.echo(f"  [4] Tell Claude what to change (re-plan)")
            click.echo(f"  [5] Abort")
            click.echo("─" * 60)
            try:
                choice = click.prompt("  Choice", type=str, default="1").strip()
            except (EOFError, KeyboardInterrupt):
                click.echo("\n  Aborted.")
                return

            if choice == "1":
                # 批准 + 自动执行（bypass edit permissions）
                # 审批通过后重新读取文件（用户可能通过选项3编辑过）
                with open(plan_path, encoding="utf-8") as f:
                    _ = f.read()
                click.echo(green("  Plan approved (auto-accept). Executing...\n"))
                _run_v2_mode(
                    mode="v2-build",
                    description=description,
                    repo_path=repo_path,
                    backend=backend,
                    registry=registry,
                    agent_config=agent_config,
                    memory_context=memory_context,
                    log_dir=log_dir,
                    intent_override=intent_override,
                    plan_approval_callback=plan_approval_callback,
                    auto_approve=True,
                    plan_file=plan_path,
                    hook_dispatcher=hook_dispatcher,
                    proactive_memory=proactive_memory,
                    renderer=renderer,
                )
                return

            elif choice == "2":
                # 批准 + 逐步确认写操作
                with open(plan_path, encoding="utf-8") as f:
                    _ = f.read()
                click.echo(green("  Plan approved (manual review). Executing...\n"))
                _run_v2_mode(
                    mode="v2-build",
                    description=description,
                    repo_path=repo_path,
                    backend=backend,
                    registry=registry,
                    agent_config=agent_config,
                    memory_context=memory_context,
                    log_dir=log_dir,
                    intent_override=intent_override,
                    plan_approval_callback=plan_approval_callback,
                    auto_approve=False,
                    plan_file=plan_path,
                    hook_dispatcher=hook_dispatcher,
                    proactive_memory=proactive_memory,
                    renderer=renderer,
                )
                return

            elif choice == "3":
                # 在编辑器中打开计划文件，原地编辑
                editor = os.environ.get("EDITOR", "notepad")
                try:
                    subprocess.call([editor, plan_path])
                except Exception:
                    click.echo(red(f"  Failed to open editor: {editor}"))
                    click.echo(dim(f"  Edit manually: {plan_path}"))
                # 读回并显示差异提示
                with open(plan_path, encoding="utf-8") as f:
                    updated = f.read()
                if updated != plan_text:
                    plan_text = updated
                    click.echo(green("  Plan updated."))
                else:
                    click.echo(dim("  No changes detected."))
                # 回到菜单，用户可以选择批准或继续改
                continue

            elif choice == "4":
                # 反馈 → 留在 plan 模式，模型重新规划
                revision_count += 1
                if revision_count >= max_revisions:
                    click.echo(yellow(f"  Max revisions ({max_revisions}) reached. Aborting."))
                    return
                try:
                    feedback = click.prompt(
                        "  What would you like to change?", type=str
                    )
                except (EOFError, KeyboardInterrupt):
                    click.echo("\n  Aborted.")
                    return
                if not feedback.strip():
                    continue
                if proactive_memory:
                    proactive_memory.check_plan_feedback(feedback)
                click.echo(dim(f"  Re-planning (revision {revision_count}/{max_revisions})...\n"))
                result = runtime.run_session(
                    session.id,
                    agent_name="plan",
                    task_description=description,
                    intent="analysis",
                    messages=[LLMMessage(
                        role="user",
                        content=(
                            f"[USER FEEDBACK ON PLAN]\n{feedback}\n\n"
                            f"Please revise the plan accordingly and output "
                            f"an updated structured plan."
                        ),
                    )],
                    max_steps_override=plan_steps,
                    budget_tokens_override=plan_budget,
                )
                # 回到循环顶部：新 plan 覆盖文件，再次展示审批菜单
                continue

            elif choice == "5":
                click.echo(dim("  Aborted. Plan saved at: ") + click.style(plan_path, fg="yellow"))
                return

            else:
                click.echo(red(f"  Invalid choice: {choice}"))
                continue
        return


def _print_v2_result(mode: str, db_path: str, session_id: str, result, *, show_summary: bool = True) -> None:
    from agent.task import RunStatus
    click.echo(dim(f"  Mode    : {mode}"))
    click.echo(dim(f"  V2 DB   : {db_path}"))
    click.echo(dim(f"  Session : {session_id}\n"))
    if show_summary and result.summary:
        click.echo(result.summary)
    if result.status == RunStatus.SUCCESS:
        click.echo(green("\n  V2 run completed successfully."))
    else:
        click.echo(yellow(f"\n  V2 run finished with status: {result.status.value}"))


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
@click.option("--mode", default="v2-build", show_default=True, type=click.Choice(["v2-build", "v2-plan"]), help="Agent mode: v2-build or v2-plan")
@click.option("--auto-approve", is_flag=True, default=False, help="Auto-approve plans without user confirmation (plan mode only)")
@click.option("--replan", is_flag=True, default=False, help="Enable one or more DAG replans after subtask failure")
@click.option("--max-replans", default=None, type=int, help="Maximum DAG replan attempts")
@click.option("--read", "read_paths", multiple=True, default=None, help="Explicitly allowed read path (repeatable)")
@click.option("--write", "write_paths", multiple=True, default=None, help="Explicitly allowed write path (repeatable)")
@click.option("--intent", "intent_override", default="auto", show_default=True, type=click.Choice(["analysis", "edit", "auto"]), help="Task intent: analysis (read-only), edit, or auto (detect)")
@click.option("--plan-file", default=None, help="Inject an approved plan file into v2-build session")
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
    replan: bool,
    max_replans: int | None,
    read_paths: tuple[str, ...] | None,
    write_paths: tuple[str, ...] | None,
    intent_override: str,
    plan_file: str | None,
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

    registry = _build_registry(
        config,
        confirm_callback=confirm_cb,
        runtime=runtime,
        memory_store=memory_store,
        external_store=external_store,
        repo_path=repo_path,
        auto_approve=auto_approve,
    )

    # ProactiveMemory（run 模式）
    proactive_memory = None
    if memory_store is not None:
        from memory.proactive import ProactiveMemory
        proactive_memory = ProactiveMemory(memory_store)
        proactive_memory.check_user_message(description)

    # Initialize HookDispatcher with ProactiveMemory as internal subscriber
    hook_dispatcher = _init_hook_dispatcher(
        repo_path, proactive_memory,
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
    from agent.factory import classify_task_intent
    from dataclasses import dataclass
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
        request_budget_tokens=config.context.request_budget_tokens,
        artifact_threshold_tokens=config.context.artifact_threshold_tokens,
        artifact_storage_dir=config.context.artifact_storage_dir,
        analysis_inspect_read_limit=config.agent.analysis_inspect_read_limit,
        analysis_verify_read_limit=config.agent.analysis_verify_read_limit,
        history_max_messages=config.context.history_window * 2,
        stream=stream,
        stream_callback=rend.stream_text if stream else None,
        thought_callback=rend.stream_thought if stream else None,
        confirm_dangerous=confirm,
        confirm_callback=confirm_cb,
    )
    # Plan 审批回调（V1 plan mode 和 V2 v2-plan 共用）

    @dataclass
    class _PlanApproval:
        approved: bool
        action: str = "execute"
        feedback: str = ""

    def _plan_approval_cb(plan_text: str):
        if auto_approve:
            rend.on_plan_generated(plan_text)
            rend.on_plan_approved()
            return _PlanApproval(approved=True)
        rend.on_plan_generated(plan_text)
        while True:
            try:
                resp = input("  [approve(y)/reject(n)/revise(e)] > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                rend.on_plan_rejected()
                return _PlanApproval(approved=False, feedback="Plan approval interrupted")
            if resp in ("y", "yes", "approve", "a", ""):
                rend.on_plan_approved()
                return _PlanApproval(approved=True)
            if resp in ("n", "no", "reject", "r"):
                rend.on_plan_rejected()
                return _PlanApproval(approved=False, feedback="Plan rejected by user")
            if resp in ("e", "edit", "revise", "feedback", "f"):
                try:
                    feedback = input("  Revision feedback > ").strip()
                except (EOFError, KeyboardInterrupt):
                    feedback = "Plan revision requested by user"
                rend.on_plan_rejected()
                if proactive_memory and feedback:
                    proactive_memory.check_plan_feedback(feedback)
                return _PlanApproval(approved=True, action="revise", feedback=feedback or "Plan revision requested by user")
            click.echo("  Please enter y to approve, n to reject, or e to request revision.")

    mcp_integration = None
    if mode in ("v2-build", "plan", "v2-plan") and getattr(config, "mcp_servers", None):
        from agent.v2 import MCPToolIntegration
        mcp_integration = MCPToolIntegration({"mcp_servers": config.mcp_servers})
        mcp_integration.initialize()
        mcp_integration.register_into(registry)

    if mode in ("v2-build", "plan", "v2-plan"):
        try:
            _run_v2_mode(
                mode=mode,
                description=description,
                repo_path=repo_path,
                backend=backend,
                registry=registry,
                agent_config=agent_config,
                memory_context=memory_context,
                log_dir=config.agent.log_dir,
                intent_override=intent_override,
                plan_approval_callback=_plan_approval_cb,
                auto_approve=auto_approve,
                plan_file=plan_file,
                hook_dispatcher=hook_dispatcher,
                proactive_memory=proactive_memory,
                mcp_integration=mcp_integration,
                renderer=rend,
            )
        finally:
            if mcp_integration is not None:
                mcp_integration.shutdown()
        flush_observability()
        return

    click.echo(red(f"Error: mode '{mode}' has been removed. Use --mode v2-build or --mode v2-plan."), err=True)
    sys.exit(1)



# ---------------------------------------------------------------------------
# chat 子命令 — 交互对话模式
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--mode", default="v2-build", show_default=True, type=click.Choice(["v2-build", "v2-plan"]), help="Agent mode")
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
                            triggers = ", ".join(s.triggers[:3]) if s.triggers else ""
                            trigger_info = f" [triggers: {triggers}]" if triggers else ""
                            click.echo(dim(f"    /{s.name:<14} — {s.description or '(no description)'}{trigger_info}"))
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
                # 检查是否是 skill 调用
                skill_cmd = user_input[1:].split()[0] if user_input[1:].strip() else ""
                if skill_registry.has_skill(skill_cmd):
                    args = user_input[1 + len(skill_cmd):].strip()
                    rendered = skill_registry.load_and_render(skill_cmd, args)
                    if rendered:
                        click.echo(dim(f"\n  Skill '{skill_cmd}' activated..."))
                        session.run_round(rendered)
                    else:
                        click.echo(dim(f"  Skill '{skill_cmd}' failed to render."))
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
    default="./logs",
    show_default=True,
    help="Load all log files from a directory when no explicit log files are provided",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON output")
def log_filters(log_files: tuple[str, ...], log_dir: str, as_json: bool) -> None:
    """Inspect session and subtask filter metadata from one or more event logs."""
    from observability.filtering import collect_log_filter_records, summarize_filter_groups

    if log_files:
        paths = [Path(log_file) for log_file in log_files]
    else:
        log_path = Path(log_dir)
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
