"""CLI operations for preserved and retained subagent worktrees."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from agent.v2.models import WorktreeResolutionAction


def _open_runtime(repo: str, *, required: bool):
    """Open isolated v2 state without constructing an LLM-capable toolset."""
    from agent.core import AgentConfig
    from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore
    from llm.base import MockBackend
    from runtime.state_paths import ProjectStatePaths, migrate_legacy_session_db
    from tools.base import ToolRegistry

    repo_path = Path(repo).expanduser().resolve()
    if not repo_path.is_dir():
        raise click.ClickException(f"Project directory not found: {repo_path}")
    paths = ProjectStatePaths.for_project(repo_path)
    migrate_legacy_session_db(repo_path, paths.sessions_db)
    if not paths.sessions_db.is_file():
        if required:
            raise click.ClickException(
                f"No v2 session state exists for project: {repo_path}"
            )
        return None
    store = SessionStore(str(paths.sessions_db))
    return SessionRuntime(
        store=store,
        backend=MockBackend([]),
        base_registry=ToolRegistry(),
        agent_registry=AgentRegistryV2(project_dir=repo_path),
        root_agent_config=AgentConfig(stream=False),
        log_dir=str(paths.logs),
    )


def _record_for(runtime, child_session_id: str):
    for record in runtime.list_managed_worktrees():
        if record.child_session_id == child_session_id:
            return record
    raise click.ClickException(
        f"No preserved or retained worktree for child session: {child_session_id}"
    )


def _echo_record(record, *, json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return
    evidence = record.evidence
    click.echo(f"Child       : {record.child_session_id}")
    click.echo(f"Parent      : {record.parent_session_id}")
    click.echo(f"Disposition : {record.disposition.value}")
    click.echo(f"Availability: {record.availability.value}")
    click.echo(f"Path        : {evidence.path}")
    click.echo(f"Revision    : {evidence.revision}")
    click.echo(f"Change      : {evidence.change.value}")
    if evidence.changed_files:
        click.echo(f"Files       : {', '.join(evidence.changed_files)}")
    if record.error:
        click.echo(f"Error       : {record.error}")


def _echo_operation(result, *, json_output: bool) -> None:
    payload = {
        "status": result.status.value,
        "evidence": result.evidence.to_dict(),
        "error": result.error,
    }
    if json_output:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        click.echo(f"Status   : {result.status.value}")
        click.echo(f"Path     : {result.evidence.path}")
        click.echo(f"Revision : {result.evidence.revision}")
        if result.error:
            click.echo(f"Error    : {result.error}")
    if not result.is_success:
        if json_output:
            raise click.exceptions.Exit(1)
        raise click.ClickException(result.error or result.status.value)


@click.group("worktree")
def worktree_admin() -> None:
    """Inspect and resolve preserved or retained subagent worktrees."""


@worktree_admin.command("list")
@click.option("--repo", "-r", default=".", show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def worktree_list(repo: str, json_output: bool) -> None:
    """List managed worktrees using Session DB state plus fresh Git facts."""
    runtime = _open_runtime(repo, required=False)
    records = runtime.list_managed_worktrees() if runtime is not None else []
    if json_output:
        click.echo(json.dumps(
            [record.to_dict() for record in records],
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not records:
        click.echo("No preserved or retained subagent worktrees.")
        return
    for index, record in enumerate(records):
        if index:
            click.echo()
        _echo_record(record, json_output=False)


@worktree_admin.command("inspect")
@click.argument("child_session_id")
@click.option("--repo", "-r", default=".", show_default=True)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def worktree_inspect(child_session_id: str, repo: str, json_output: bool) -> None:
    """Inspect fresh Git facts for one managed child worktree."""
    runtime = _open_runtime(repo, required=True)
    record = _record_for(runtime, child_session_id)
    _echo_record(record, json_output=json_output)
    if record.error:
        if json_output:
            raise click.exceptions.Exit(1)
        raise click.ClickException(record.error)


def _resolve(
    *,
    child_session_id: str,
    repo: str,
    revision: str,
    action: "WorktreeResolutionAction",
    assume_yes: bool,
    json_output: bool,
) -> None:
    from agent.v2.models import WorktreeResolutionAction

    runtime = _open_runtime(repo, required=True)
    record = _record_for(runtime, child_session_id)
    if record.error:
        raise click.ClickException(record.error)
    if not assume_yes and not click.confirm(
        f"{action.value.capitalize()} worktree {record.evidence.path} at revision {revision}?"
    ):
        raise click.Abort()
    if action is WorktreeResolutionAction.APPLY:
        result = runtime.apply_subagent_worktree(
            record.parent_session_id,
            child_session_id,
            expected_revision=revision,
        )
    elif action is WorktreeResolutionAction.DISCARD:
        result = runtime.discard_subagent_worktree(
            record.parent_session_id,
            child_session_id,
            expected_revision=revision,
        )
    else:
        raise TypeError(f"Unsupported worktree resolution action: {action!r}")
    _echo_operation(result, json_output=json_output)


@worktree_admin.command("apply")
@click.argument("child_session_id")
@click.option("--revision", required=True, help="Exact revision returned by inspect")
@click.option("--repo", "-r", default=".", show_default=True)
@click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def worktree_apply(
    child_session_id: str, revision: str, repo: str,
    assume_yes: bool, json_output: bool,
) -> None:
    """Apply an exact reviewed worktree revision to the parent branch."""
    from agent.v2.models import WorktreeResolutionAction
    _resolve(
        child_session_id=child_session_id,
        repo=repo,
        revision=revision,
        action=WorktreeResolutionAction.APPLY,
        assume_yes=assume_yes,
        json_output=json_output,
    )


@worktree_admin.command("discard")
@click.argument("child_session_id")
@click.option("--revision", required=True, help="Exact revision returned by inspect")
@click.option("--repo", "-r", default=".", show_default=True)
@click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON")
def worktree_discard(
    child_session_id: str, revision: str, repo: str,
    assume_yes: bool, json_output: bool,
) -> None:
    """Permanently discard an exact reviewed worktree revision."""
    from agent.v2.models import WorktreeResolutionAction
    _resolve(
        child_session_id=child_session_id,
        repo=repo,
        revision=revision,
        action=WorktreeResolutionAction.DISCARD,
        assume_yes=assume_yes,
        json_output=json_output,
    )
