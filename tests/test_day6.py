"""
tests/test_day6.py

Day 6 测试：config/schema.py、entry/cli.py（Click test runner）。
GitHub Issue 入口依赖网络，只测纯逻辑部分。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from config.schema import (
    AppConfig, load_config, merge_cli_overrides, _expand_env, _parse,
)
from entry.cli import cli


# ===========================================================================
# config/schema.py
# ===========================================================================

class TestExpandEnv:
    def test_expands_set_variable(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret123")
        assert _expand_env("${MY_KEY}") == "secret123"

    def test_unset_variable_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert _expand_env("${MISSING_VAR}") == ""

    def test_no_placeholder_unchanged(self):
        assert _expand_env("hello world") == "hello world"

    def test_multiple_placeholders(self, monkeypatch):
        monkeypatch.setenv("A", "foo")
        monkeypatch.setenv("B", "bar")
        assert _expand_env("${A}-${B}") == "foo-bar"


class TestParseConfig:
    def test_defaults_when_empty(self):
        config = _parse({})
        assert config.llm.provider == ""       # empty = must configure
        assert config.agent.max_steps == 40
        assert config.tools.shell.timeout == 30
        assert config.context.history_window == 20

    def test_llm_section(self):
        config = _parse({"llm": {"provider": "deepseek", "model": "deepseek-chat"}})
        assert config.llm.provider == "deepseek"
        assert config.llm.model == "deepseek-chat"

    def test_agent_section(self):
        config = _parse({"agent": {"max_steps": 20, "budget_tokens": 40000}})
        assert config.agent.max_steps == 20
        assert config.agent.budget_tokens == 40000

    def test_tools_section(self):
        config = _parse({"tools": {"shell": {"timeout": 60}}})
        assert config.tools.shell.timeout == 60

    def test_context_section(self):
        config = _parse({"context": {"history_window": 10}})
        assert config.context.history_window == 10

    def test_partial_section_uses_defaults(self):
        config = _parse({"llm": {"provider": "openai"}})
        assert config.llm.provider == "openai"
        assert config.llm.model == ""                     # not specified = empty default

    def test_base_url_none_becomes_empty(self):
        config = _parse({"llm": {"base_url": None}})
        assert config.llm.base_url == ""


class TestLoadConfig:
    def test_load_from_file(self, tmp_path):
        yaml_content = """
llm:
  provider: deepseek
  model: deepseek-chat
  api_key: sk-test
agent:
  max_steps: 15
"""
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml_content)
        config = load_config(config_file)
        assert config.llm.provider == "deepseek"
        assert config.agent.max_steps == 15

    def test_env_var_expanded_in_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-from-env")
        yaml_content = "llm:\n  api_key: ${TEST_API_KEY}\n"
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml_content)
        config = load_config(config_file)
        assert config.llm.api_key == "sk-from-env"

    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert isinstance(config, AppConfig)
        assert config.llm.provider == ""           # empty defaults when config missing

    def test_none_path_returns_defaults(self, tmp_path, monkeypatch):
        # 确保当前目录没有 default.yaml
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert isinstance(config, AppConfig)


class TestMergeCliOverrides:
    def test_override_model(self):
        config = _parse({})
        config = merge_cli_overrides(config, model="gpt-4o")
        assert config.llm.model == "gpt-4o"

    def test_override_provider(self):
        config = _parse({})
        config = merge_cli_overrides(config, provider="openai")
        assert config.llm.provider == "openai"

    def test_override_max_steps(self):
        config = _parse({})
        config = merge_cli_overrides(config, max_steps=10)
        assert config.agent.max_steps == 10

    def test_none_values_not_applied(self):
        config = _parse({"agent": {"max_steps": 25}})
        config = merge_cli_overrides(config, max_steps=None)
        assert config.agent.max_steps == 25   # 未被覆盖

    def test_override_api_key(self):
        config = _parse({})
        config = merge_cli_overrides(config, api_key="sk-override")
        assert config.llm.api_key == "sk-override"


# ===========================================================================
# entry/cli.py — Click runner 测试
# ===========================================================================

class TestCliHelp:
    def test_root_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Coding Agent" in result.output

    def test_run_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--repo" in result.output
        assert "--task" in result.output
        assert "--model" in result.output
        assert "--mode" in result.output

    def test_log_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "--help"])
        assert result.exit_code == 0

    def test_log_show_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "show", "--help"])
        assert result.exit_code == 0

    def test_log_list_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "list", "--help"])
        assert result.exit_code == 0


class TestCliRun:
    """使用 MockBackend 测试 run 命令，不消耗真实 API。"""

    def _invoke_run(self, tmp_path, task="fix it", extra_args=None):
        """辅助：用 MockBackend 跑 CLI run 命令。"""
        from agent.task import Action, ActionType
        from llm.base import MockBackend

        script = [Action(ActionType.FINISH, "done", message="Task complete")]
        mock_backend = MockBackend(script)

        runner = CliRunner()
        args = [
            "run",
            "--repo", str(tmp_path),
            "--task", task,
        ]
        if extra_args:
            args.extend(extra_args)

        # patch create_backend_from_config 返回 mock
        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                from config.schema import AppConfig, AgentCfg, LLMConfig, ContextConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(cli, args, obj={})

        return result

    def test_run_succeeds(self, tmp_path):
        result = self._invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        assert "SUCCESS" in result.output

    def test_run_shows_model_info(self, tmp_path):
        result = self._invoke_run(tmp_path)
        assert "Model" in result.output or "Provider" in result.output

    def test_run_missing_task_fails(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--repo", str(tmp_path)], obj={})
        assert result.exit_code != 0

    def test_run_nonexistent_repo_fails(self, tmp_path):
        runner = CliRunner()
        with patch("entry.cli.load_config") as mock_cfg:
            from config.schema import AppConfig
            mock_cfg.return_value = AppConfig()
            with patch("entry.cli.create_backend_from_config", return_value=MagicMock()):
                result = runner.invoke(cli, [
                    "run",
                    "--repo", str(tmp_path / "no_such_dir"),
                    "--task", "fix it",
                ], obj={})
        assert result.exit_code != 0

    def test_run_from_task_file(self, tmp_path):
        task_file = tmp_path / "task.txt"
        task_file.write_text("Fix the parser bug")
        result = self._invoke_run(tmp_path, extra_args=["--task-file", str(task_file)])
        # task-file 和 --task 不能同时用，这里只传 task-file
        # 重新 invoke，不带 --task
        from agent.task import Action, ActionType
        from llm.base import MockBackend
        script = [Action(ActionType.FINISH, "done", message="ok")]
        mock_backend = MockBackend(script)
        runner = CliRunner()
        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(cli, [
                    "run",
                    "--repo", str(tmp_path),
                    "--task-file", str(task_file),
                ], obj={})
        assert result.exit_code == 0


class TestCliLog:
    def test_log_show(self, tmp_path):
        """写一个真实 event log，用 CLI 读取。"""
        from agent.event_log import EventLog
        from agent.task import Task, Action, ActionType
        from llm.base import MockBackend
        from agent.core import Agent
        from tools.base import NoopTool, ToolRegistry

        task = Task(
            task_id="logtest1",
            description="test task",
            repo_path=str(tmp_path),
        )
        registry = ToolRegistry().register(NoopTool("shell"))
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)
            log_path = log.path

        runner = CliRunner()
        result = runner.invoke(cli, ["log", "show", str(log_path)], obj={})
        assert result.exit_code == 0
        assert "Total events" in result.output

    def test_log_list(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "abc_20240101.jsonl").write_text('{"event_type":"task_start"}\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["log", "list", "--dir", str(log_dir)], obj={})
        assert result.exit_code == 0
        assert "abc_20240101.jsonl" in result.output

    def test_log_list_empty(self, tmp_path):
        log_dir = tmp_path / "empty_logs"
        log_dir.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "list", "--dir", str(log_dir)], obj={})
        assert result.exit_code == 0
        assert "No log files" in result.output


# ===========================================================================
# entry/github_issue.py — 纯逻辑测试（不发真实网络请求）
# ===========================================================================

class TestGitHubIssueLogic:
    def test_fetch_issue_no_token_raises(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from entry.github_issue import fetch_issue
        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            fetch_issue("owner/repo", 1)

    def test_fetch_issue_mocked(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        from entry.github_issue import fetch_issue

        mock_issue = MagicMock()
        mock_issue.title = "Fix the parser"
        mock_issue.body = "The parser crashes on empty input"
        mock_issue.html_url = "https://github.com/owner/repo/issues/1"

        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        with patch("entry.github_issue._get_github_client", return_value=mock_gh):
            title, body, url = fetch_issue("owner/repo", 1)

        assert title == "Fix the parser"
        assert "empty input" in body
        assert "issues/1" in url

    def test_create_pr_mocked(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        from entry.github_issue import create_pull_request

        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/owner/repo/pull/99"

        mock_repo = MagicMock()
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.create_pull.return_value = mock_pr

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        with patch("entry.github_issue._get_github_client", return_value=mock_gh):
            url = create_pull_request("owner/repo", "agent/fix-1", "Fix it", "body")

        assert "pull/99" in url