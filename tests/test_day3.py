"""
tests/test_day3.py

Day 3 工具层测试。使用真实文件系统和 subprocess。
不 mock 任何东西——工具的价值就在于真实执行。

fixtures/ 下放测试用 repo 片段，测试用 tmp_path 隔离，互不干扰。
"""

import subprocess
from pathlib import Path

import pytest

from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool
from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
from tools.shell_tool import ShellTool, _check_blocked
from tools.utils import truncate_output
from tools.test_tool import PytestTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_file(tmp_path) -> Path:
    """包含 20 行内容的示例文件。"""
    f = tmp_path / "sample.py"
    lines = [f"# line {i}\nresult_{i} = {i}" for i in range(10)]
    f.write_text("\n".join(lines))
    return f


@pytest.fixture
def large_file(tmp_path) -> Path:
    """超过 MAX_READ_LINES 行的大文件。"""
    f = tmp_path / "large.py"
    f.write_text("\n".join(f"x = {i}" for i in range(600)))
    return f


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """初始化一个最简 git repo，配置好用户信息。"""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path, capture_output=True,
    )
    return tmp_path


@pytest.fixture
def pytest_repo(tmp_path) -> Path:
    """包含一个会失败和一个会通过的测试的 repo。"""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_sample.py").write_text(
        "def test_pass():\n    assert 1 == 1\n\n"
        "def test_fail():\n    assert 1 == 2\n"
    )
    return tmp_path


# ===========================================================================
# FileReadTool
# ===========================================================================

class TestFileReadTool:
    tool = FileReadTool()

    def test_read_existing_file(self, sample_file):
        result = self.tool.execute({"path": str(sample_file)})
        assert result.success
        assert "line 0" in result.output
        assert "1 |" in result.output     # 行号格式

    def test_read_nonexistent_file(self, tmp_path):
        result = self.tool.execute({"path": str(tmp_path / "ghost.py")})
        assert not result.success
        assert "not found" in result.error.lower()

    def test_read_directory_fails(self, tmp_path):
        result = self.tool.execute({"path": str(tmp_path)})
        assert not result.success

    def test_large_file_truncated(self, large_file):
        result = self.tool.execute({"path": str(large_file)})
        assert result.success
        assert "more lines not shown" in result.output
        assert "file_view" in result.output   # 提示用 file_view

    def test_shows_total_line_count(self, large_file):
        result = self.tool.execute({"path": str(large_file)})
        assert "600 lines total" in result.output

    def test_tool_schema_valid(self):
        schema = self.tool.to_llm_schema()
        assert schema.name == "file_read"
        assert "path" in schema.parameters["properties"]


# ===========================================================================
# FileViewTool
# ===========================================================================

class TestFileViewTool:
    tool = FileViewTool()

    def test_view_from_start(self, large_file):
        result = self.tool.execute({"path": str(large_file), "start_line": 1})
        assert result.success
        assert "1 |" in result.output
        assert "100 |" in result.output
        assert "101 |" not in result.output   # 窗口只有 100 行

    def test_view_middle_section(self, large_file):
        result = self.tool.execute({"path": str(large_file), "start_line": 200})
        assert result.success
        assert "200 |" in result.output

    def test_view_shows_navigation_hint(self, large_file):
        result = self.tool.execute({"path": str(large_file), "start_line": 1})
        assert "Next:" in result.output or "End of file" in result.output

    def test_view_last_window_shows_end(self, large_file):
        result = self.tool.execute({"path": str(large_file), "start_line": 550})
        assert "End of file" in result.output

    def test_view_start_line_beyond_eof(self, sample_file):
        result = self.tool.execute({"path": str(sample_file), "start_line": 9999})
        assert not result.success
        assert "exceeds" in result.error.lower()

    def test_view_nonexistent_file(self, tmp_path):
        result = self.tool.execute({"path": str(tmp_path / "missing.py")})
        assert not result.success


# ===========================================================================
# FileWriteTool
# ===========================================================================

class TestFileWriteTool:
    tool = FileWriteTool()

    def test_write_new_file(self, tmp_path):
        path = tmp_path / "new.py"
        result = self.tool.execute({"path": str(path), "content": "x = 1\ny = 2\n"})
        assert result.success
        assert path.read_text() == "x = 1\ny = 2\n"

    def test_write_overwrites_existing(self, tmp_path):
        path = tmp_path / "existing.py"
        path.write_text("old content")
        self.tool.execute({"path": str(path), "content": "new content"})
        assert path.read_text() == "new content"

    def test_write_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "file.py"
        result = self.tool.execute({"path": str(path), "content": "hello"})
        assert result.success
        assert path.exists()

    def test_write_returns_line_count(self, tmp_path):
        path = tmp_path / "f.py"
        result = self.tool.execute({"path": str(path), "content": "a\nb\nc\n"})
        assert "3 lines" in result.output

    def test_write_empty_content(self, tmp_path):
        path = tmp_path / "empty.py"
        result = self.tool.execute({"path": str(path), "content": ""})
        assert result.success
        assert path.read_text() == ""


# ===========================================================================
# ShellTool
# ===========================================================================

class TestShellTool:
    tool = ShellTool()

    def test_simple_command(self):
        result = self.tool.execute({"cmd": "echo hello"})
        assert result.success
        assert "hello" in result.output

    def test_failed_command(self):
        result = self.tool.execute({"cmd": "false"})
        assert not result.success
        assert "Exit code" in result.error

    def test_timeout(self):
        result = self.tool.execute({"cmd": "sleep 10", "timeout": 1})
        assert not result.success
        assert "timed out" in result.error.lower()

    def test_stderr_captured(self):
        result = self.tool.execute({"cmd": "echo err >&2"})
        assert "err" in result.output

    def test_empty_cmd_fails(self):
        result = self.tool.execute({"cmd": ""})
        assert not result.success

    def test_cwd_respected(self, tmp_path):
        result = self.tool.execute({"cmd": "pwd", "cwd": str(tmp_path)})
        assert result.success
        assert str(tmp_path) in result.output


class TestShellBlacklist:
    def test_rm_rf_root_blocked(self):
        assert _check_blocked("rm -rf /") is not None

    def test_rm_rf_home_blocked(self):
        assert _check_blocked("rm -rf ~") is not None

    def test_normal_rm_allowed(self):
        assert _check_blocked("rm myfile.txt") is None

    def test_echo_allowed(self):
        assert _check_blocked("echo hello") is None

    def test_case_insensitive(self):
        assert _check_blocked("RM -RF /") is not None


class TestShellTruncate:
    def test_short_output_unchanged(self):
        assert truncate_output("hello", 100) == "hello"

    def test_long_output_truncated(self):
        text = "x" * 10_000
        result = truncate_output(text, 1_000)
        assert len(result) < len(text)
        assert "truncated" in result

    def test_head_and_tail_preserved(self):
        head = "START" * 200
        tail = "END" * 200
        text = head + "MIDDLE" * 1000 + tail
        result = truncate_output(text, 500)
        assert result.startswith("START")
        assert result.endswith("END" * (200 // len("END")))


# ===========================================================================
# SearchTextTool
# ===========================================================================

class TestSearchTextTool:
    tool = SearchTextTool()

    def test_find_pattern_in_file(self, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    pass\ndef bar():\n    return 1\n")
        result = self.tool.execute({"pattern": "def ", "path": str(tmp_path)})
        assert result.success
        assert "foo" in result.output
        assert "bar" in result.output

    def test_no_matches(self, tmp_path):
        (tmp_path / "f.py").write_text("hello world")
        result = self.tool.execute({"pattern": "ZZZNOMATCH", "path": str(tmp_path)})
        assert result.success
        assert "No matches" in result.output

    def test_file_pattern_filter(self, tmp_path):
        (tmp_path / "code.py").write_text("target here")
        (tmp_path / "data.txt").write_text("target here")
        result = self.tool.execute({
            "pattern": "target",
            "path": str(tmp_path),
            "file_pattern": "*.py",
        })
        assert "code.py" in result.output
        assert "data.txt" not in result.output

    def test_case_insensitive(self, tmp_path):
        (tmp_path / "f.py").write_text("Hello World")
        result = self.tool.execute({
            "pattern": "hello",
            "path": str(tmp_path),
            "case_sensitive": False,
        })
        assert "Hello World" in result.output

    def test_invalid_regex(self, tmp_path):
        result = self.tool.execute({"pattern": "[invalid", "path": str(tmp_path)})
        assert not result.success
        assert "Invalid regex" in result.error

    def test_includes_line_numbers(self, tmp_path):
        (tmp_path / "f.py").write_text("line one\nline two\nline three\n")
        result = self.tool.execute({"pattern": "two", "path": str(tmp_path)})
        assert ":2:" in result.output


# ===========================================================================
# FindFilesTool
# ===========================================================================

class TestFindFilesTool:
    tool = FindFilesTool()

    def test_find_py_files(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        result = self.tool.execute({"pattern": "*.py", "path": str(tmp_path)})
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    def test_no_files_found(self, tmp_path):
        result = self.tool.execute({"pattern": "*.xyz", "path": str(tmp_path)})
        assert "No files found" in result.output

    def test_skips_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.py").write_text("")
        (tmp_path / "real.py").write_text("")
        result = self.tool.execute({"pattern": "*.py", "path": str(tmp_path)})
        assert "real.py" in result.output
        assert ".git" not in result.output

    def test_nonexistent_path(self, tmp_path):
        result = self.tool.execute({"pattern": "*.py", "path": str(tmp_path / "no")})
        assert not result.success


# ===========================================================================
# FindSymbolTool
# ===========================================================================

class TestFindSymbolTool:
    tool = FindSymbolTool()

    def test_find_function(self, tmp_path):
        (tmp_path / "module.py").write_text(
            "def my_function():\n    pass\n\nclass MyClass:\n    pass\n"
        )
        result = self.tool.execute({"symbol": "my_function", "path": str(tmp_path)})
        assert result.success
        assert "my_function" in result.output
        assert "def" in result.output

    def test_find_class(self, tmp_path):
        (tmp_path / "module.py").write_text("class MyClass:\n    pass\n")
        result = self.tool.execute({"symbol": "MyClass", "path": str(tmp_path)})
        assert "MyClass" in result.output
        assert "class" in result.output

    def test_partial_match(self, tmp_path):
        (tmp_path / "module.py").write_text(
            "def process_data():\n    pass\ndef process_image():\n    pass\n"
        )
        result = self.tool.execute({"symbol": "process", "path": str(tmp_path)})
        assert "process_data" in result.output
        assert "process_image" in result.output

    def test_method_detected_as_method(self, tmp_path):
        (tmp_path / "module.py").write_text(
            "class Foo:\n    def bar(self):\n        pass\n"
        )
        result = self.tool.execute({"symbol": "bar", "path": str(tmp_path)})
        assert "method" in result.output

    def test_no_symbol_found(self, tmp_path):
        (tmp_path / "module.py").write_text("x = 1\n")
        result = self.tool.execute({"symbol": "ghost", "path": str(tmp_path)})
        assert "No definition found" in result.output

    def test_includes_line_number(self, tmp_path):
        (tmp_path / "module.py").write_text("# header\ndef target():\n    pass\n")
        result = self.tool.execute({"symbol": "target", "path": str(tmp_path)})
        assert ":2:" in result.output


# ===========================================================================
# PytestTool
# ===========================================================================

class TestTestTool:
    tool = PytestTool()

    def test_passing_tests(self, tmp_path):
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_pass.py").write_text("def test_ok():\n    assert True\n")
        result = self.tool.execute({"path": str(tests), "cwd": str(tmp_path)})
        assert result.success
        assert result.error is None

    def test_failing_tests(self, pytest_repo):
        result = self.tool.execute({
            "path": str(pytest_repo / "tests"),
            "cwd": str(pytest_repo),
        })
        assert not result.success
        assert "test_fail" in result.output

    def test_output_contains_failure_info(self, pytest_repo):
        result = self.tool.execute({
            "path": str(pytest_repo / "tests"),
            "cwd": str(pytest_repo),
        })
        # 应该包含失败的测试名或断言错误信息
        assert "fail" in result.output.lower() or "assert" in result.output.lower()

    def test_nonexistent_path(self, tmp_path):
        result = self.tool.execute({"path": str(tmp_path / "no_tests")})
        # pytest 找不到测试文件会报 error，不应崩溃
        assert isinstance(result.success, bool)

    def test_extra_args_passed(self, tmp_path):
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_x.py").write_text("def test_ok():\n    assert True\n")
        result = self.tool.execute({
            "path": str(tests),
            "args": "-v",
            "cwd": str(tmp_path),
        })
        assert isinstance(result.success, bool)


# ===========================================================================
# GitStatusTool
# ===========================================================================

class TestGitStatusTool:
    tool = GitStatusTool()

    def test_clean_repo(self, git_repo):
        # 先提交一个文件，才能是 clean
        (git_repo / "f.py").write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=git_repo, capture_output=True,
        )
        result = self.tool.execute({"cwd": str(git_repo)})
        assert result.success

    def test_modified_file_shown(self, git_repo):
        f = git_repo / "f.py"
        f.write_text("x = 1")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=git_repo, capture_output=True,
        )
        f.write_text("x = 2")   # 修改
        result = self.tool.execute({"cwd": str(git_repo)})
        assert result.success
        assert "f.py" in result.output


# ===========================================================================
# GitDiffTool
# ===========================================================================

class TestGitDiffTool:
    tool = GitDiffTool()

    def test_diff_shows_changes(self, git_repo):
        f = git_repo / "f.py"
        f.write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=git_repo, capture_output=True,
        )
        f.write_text("x = 2\n")
        result = self.tool.execute({"cwd": str(git_repo)})
        assert result.success
        assert "-x = 1" in result.output or "+x = 2" in result.output

    def test_no_changes_message(self, git_repo):
        f = git_repo / "f.py"
        f.write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=git_repo, capture_output=True,
        )
        result = self.tool.execute({"cwd": str(git_repo)})
        assert result.success
        assert "No" in result.output


# ===========================================================================
# GitAddTool + GitCommitTool
# ===========================================================================

class TestGitAddAndCommit:
    add_tool = GitAddTool()
    commit_tool = GitCommitTool()

    def test_add_and_commit(self, git_repo):
        (git_repo / "f.py").write_text("x = 1\n")
        add_result = self.add_tool.execute({"cwd": str(git_repo)})
        assert add_result.success

        commit_result = self.commit_tool.execute({
            "message": "add f.py",
            "cwd": str(git_repo),
        })
        assert commit_result.success
        assert "f.py" in commit_result.output or "add" in commit_result.output.lower()

    def test_commit_without_message_fails(self, git_repo):
        result = self.commit_tool.execute({"message": "", "cwd": str(git_repo)})
        assert not result.success
        assert "message" in result.error.lower()

    def test_commit_nothing_staged_fails(self, git_repo):
        # 初始化空 repo，没有任何 staged 文件
        result = self.commit_tool.execute({
            "message": "empty commit",
            "cwd": str(git_repo),
        })
        assert not result.success