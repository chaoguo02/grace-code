"""
entry/github_issue.py

GitHub Issue 自动修复入口。

流程：
1. 拉取 Issue 标题 + body 作为任务描述
2. Clone 或使用已有的本地 repo
3. 在新分支上运行 agent
4. agent 完成后创建 PR（可选）

用法：
    python -m entry.github_issue \
        --repo owner/repo \
        --issue 42 \
        --local-path /tmp/myrepo

依赖：
    pip install PyGithub gitpython
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import click

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GitHub 操作
# ---------------------------------------------------------------------------

def _get_github_client():
    """初始化 PyGithub 客户端，从环境变量读 token。"""
    try:
        from github import Github
    except ImportError:
        raise ImportError("PyGithub not installed. Run: pip install PyGithub")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN environment variable is not set.\n"
            "Create a token at https://github.com/settings/tokens"
        )
    return Github(token)


def fetch_issue(repo_name: str, issue_number: int) -> tuple[str, str, str]:
    """
    拉取 GitHub Issue 内容。

    Returns:
        (title, body, html_url)
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_number)
    return issue.title, issue.body or "", issue.html_url


def create_pull_request(
    repo_name: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
) -> str:
    """
    创建 PR，返回 PR URL。

    Args:
        repo_name: "owner/repo" 格式
        branch:    源分支（agent 在这个分支上做了修改）
        title:     PR 标题
        body:      PR 描述
        base:      目标分支，默认 main
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_name)

    # 检查 base 分支是否存在，不存在时尝试 master
    try:
        repo.get_branch(base)
    except Exception:
        base = "master"

    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch,
        base=base,
    )
    return pr.html_url


# ---------------------------------------------------------------------------
# Git 操作
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str) -> tuple[bool, str]:
    """运行 git 命令，返回 (success, output)。"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd,
            encoding="utf-8",
            errors="replace",
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output
    except Exception as e:
        return False, str(e)


def clone_repo(repo_name: str, local_path: str) -> None:
    """Clone repo 到本地路径（如果已存在则跳过）。"""
    path = Path(local_path)
    if path.exists() and (path / ".git").exists():
        logger.info("Repo already exists at %s, skipping clone", local_path)
        return

    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        url = f"https://{token}@github.com/{repo_name}.git"
    else:
        url = f"https://github.com/{repo_name}.git"

    click.echo(f"Cloning {repo_name} → {local_path} ...")
    ok, out = _run_git(["clone", url, local_path], cwd="/tmp")
    if not ok:
        raise RuntimeError(f"git clone failed: {out}")


def create_branch(local_path: str, branch: str) -> None:
    """创建并切换到新分支。"""
    ok, out = _run_git(["checkout", "-b", branch], cwd=local_path)
    if not ok:
        # 分支已存在，切换过去
        _run_git(["checkout", branch], cwd=local_path)


def push_branch(local_path: str, branch: str) -> None:
    """推送分支到远端。"""
    ok, out = _run_git(
        ["push", "--set-upstream", "origin", branch],
        cwd=local_path,
    )
    if not ok:
        raise RuntimeError(f"git push failed: {out}")


# ---------------------------------------------------------------------------
# 核心流程
# ---------------------------------------------------------------------------

def run_on_issue(
    repo_name: str,
    issue_number: int,
    local_path: str,
    config_path: str | None = None,
    create_pr: bool = True,
    base_branch: str = "main",
) -> int:
    """
    拉取 Issue，运行 agent，创建 PR。

    Returns:
        0 if success, 1 if failed
    """
    from config.schema import load_config
    from agent.core import AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    from agent.factory import create_agent
    from llm.router import create_backend_from_config

    config = load_config(config_path)

    # 1. 拉取 Issue
    click.echo(f"\nFetching issue #{issue_number} from {repo_name} ...")
    try:
        title, body, issue_url = fetch_issue(repo_name, issue_number)
    except Exception as e:
        click.echo(f"Error fetching issue: {e}", err=True)
        return 1

    click.echo(f"  Title: {title}")
    description = f"Fix GitHub Issue #{issue_number}: {title}\n\n{body}"

    # 2. Clone（如果需要）
    try:
        clone_repo(repo_name, local_path)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        return 1

    # 3. 创建工作分支
    branch = f"agent/fix-issue-{issue_number}-{int(time.time())}"
    create_branch(local_path, branch)
    click.echo(f"  Branch: {branch}")

    # 4. 构建 agent
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        return 1

    from entry.cli import _build_registry

    # 初始化记忆系统
    memory_store = None
    memory_context = None
    if config.memory.enabled:
        from memory.store import MemoryStore
        from memory.context import MemoryContext
        memory_store = MemoryStore(
            repo_path=local_path,
            memory_dir=config.memory.directory or None,
            max_index_lines=config.memory.max_index_lines,
        )
        memory_context = MemoryContext(
            store=memory_store,
            max_lines=config.memory.max_index_lines,
            enabled=config.memory.enabled,
        )

    registry = _build_registry(config, memory_store=memory_store)

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )
    agent = create_agent("auto", backend, registry, agent_config, task_description=description, memory_context=memory_context)

    task = Task(
        description=description,
        repo_path=local_path,
        issue_url=issue_url,
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    # 5. 运行 agent
    click.echo(f"\nRunning agent on issue #{issue_number} ...")
    t0 = time.time()
    with EventLog.create(task, log_dir=config.agent.log_dir) as log:
        result = agent.run(task, log)

    elapsed = time.time() - t0
    click.echo(f"  Status : {result.status.value}")
    click.echo(f"  Steps  : {result.steps_taken}")
    click.echo(f"  Tokens : {result.total_tokens:,}")
    click.echo(f"  Time   : {elapsed:.1f}s")

    if not result.is_success():
        click.echo(f"  Agent did not complete the task.", err=True)
        return 1

    # 6. Push 分支
    if create_pr:
        click.echo("\nPushing branch ...")
        try:
            push_branch(local_path, branch)
        except RuntimeError as e:
            click.echo(f"Warning: push failed: {e}", err=True)
            click.echo("Changes are committed locally. Push manually to create a PR.")
            return 0

        # 7. 创建 PR
        pr_title = f"[Agent] Fix issue #{issue_number}: {title}"
        pr_body = (
            f"Fixes #{issue_number}\n\n"
            f"This PR was automatically generated by the coding agent.\n\n"
            f"## Summary\n{result.summary}\n\n"
            f"## Task\n{description[:500]}"
        )
        try:
            pr_url = create_pull_request(
                repo_name=repo_name,
                branch=branch,
                title=pr_title,
                body=pr_body,
                base=base_branch,
            )
            click.echo(f"\n✓ PR created: {pr_url}\n")
        except Exception as e:
            click.echo(f"Warning: PR creation failed: {e}", err=True)
            click.echo(f"Branch pushed. Create PR manually from branch: {branch}")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--repo", "-r", required=True, help="GitHub repo (owner/repo)")
@click.option("--issue", "-i", required=True, type=int, help="Issue number")
@click.option(
    "--local-path", "-l", required=True,
    help="Local path to clone/use the repo",
)
@click.option("--config", "-c", default=None, help="Config YAML path")
@click.option("--no-pr", is_flag=True, help="Skip PR creation")
@click.option("--base-branch", default="main", help="Base branch for PR (default: main)")
@click.option("--verbose", "-v", is_flag=True)
def main(
    repo: str,
    issue: int,
    local_path: str,
    config: str | None,
    no_pr: bool,
    base_branch: str,
    verbose: bool,
) -> None:
    """Run the coding agent on a GitHub issue and create a PR."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    sys.exit(run_on_issue(
        repo_name=repo,
        issue_number=issue,
        local_path=local_path,
        config_path=config,
        create_pr=not no_pr,
        base_branch=base_branch,
    ))


if __name__ == "__main__":
    main()