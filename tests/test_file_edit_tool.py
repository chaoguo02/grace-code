"""
tests/test_file_edit_tool.py

FileEditTool 单元测试。
"""

import pytest
from pathlib import Path

from tools.file_edit_tool import FileEditTool


@pytest.fixture
def tool():
    return FileEditTool()


@pytest.fixture
def sample_file(tmp_path):
    """创建包含示例代码的临时文件。"""
    f = tmp_path / "sample.py"
    f.write_text(
        "def hello():\n"
        "    print('hello')\n"
        "\n"
        "def world():\n"
        "    print('world')\n"
        "\n"
        "def main():\n"
        "    hello()\n"
        "    world()\n",
        encoding="utf-8",
    )
    return f


class TestNormalEdit:
    """正常替换场景。"""

    def test_single_match_replaces(self, tool, sample_file):
        result = tool.execute({
            "path": str(sample_file),
            "old_str": "def hello():\n    print('hello')",
            "new_str": "def hello():\n    print('hi there')",
        })
        assert result.success
        assert "line 1" in result.output
        content = sample_file.read_text(encoding="utf-8")
        assert "print('hi there')" in content
        assert "print('hello')" not in content

    def test_delete_content(self, tool, sample_file):
        """new_str 为空时，删除 old_str。"""
        result = tool.execute({
            "path": str(sample_file),
            "old_str": "\ndef world():\n    print('world')\n",
            "new_str": "",
        })
        assert result.success
        content = sample_file.read_text(encoding="utf-8")
        assert "def world():" not in content

    def test_insert_content(self, tool, sample_file):
        """通过把 old_str 包含并扩展来插入内容。"""
        result = tool.execute({
            "path": str(sample_file),
            "old_str": "def main():\n    hello()\n    world()",
            "new_str": "def main():\n    hello()\n    world()\n    print('done')",
        })
        assert result.success
        content = sample_file.read_text(encoding="utf-8")
        assert "print('done')" in content

    def test_reports_line_delta(self, tool, sample_file):
        """输出应报告行数变化。"""
        result = tool.execute({
            "path": str(sample_file),
            "old_str": "def hello():\n    print('hello')",
            "new_str": "def hello():\n    # greeting\n    print('hello')\n    return True",
        })
        assert result.success
        assert "+2 lines" in result.output


class TestErrorCases:
    """各种错误情况。"""

    def test_no_match(self, tool, sample_file):
        result = tool.execute({
            "path": str(sample_file),
            "old_str": "this string does not exist",
            "new_str": "replacement",
        })
        assert not result.success
        assert "not found" in result.error

    def test_no_match_whitespace_hint(self, tool, tmp_path):
        """匹配 strip 后的内容但原始不匹配时，给出提示。"""
        f = tmp_path / "ws.py"
        f.write_text("    indented_line = 1\n    other = 2\n", encoding="utf-8")
        # old_str 加了换行使得它不是子串，但 strip 后能匹配
        result = tool.execute({
            "path": str(f),
            "old_str": "indented_line = 1\nother = 2",  # no leading spaces
            "new_str": "replacement",
        })
        assert not result.success
        assert "indentation" in result.error

    def test_multiple_matches(self, tool, tmp_path):
        """多处匹配时报错并显示行号。"""
        f = tmp_path / "dup.py"
        f.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
        result = tool.execute({
            "path": str(f),
            "old_str": "x = 1",
            "new_str": "x = 2",
        })
        assert not result.success
        assert "3 locations" in result.error
        assert "lines:" in result.error

    def test_file_not_found(self, tool, tmp_path):
        result = tool.execute({
            "path": str(tmp_path / "nonexistent.py"),
            "old_str": "something",
            "new_str": "else",
        })
        assert not result.success
        assert "not found" in result.error.lower()

    def test_not_a_file(self, tool, tmp_path):
        result = tool.execute({
            "path": str(tmp_path),
            "old_str": "something",
            "new_str": "else",
        })
        assert not result.success
        assert "Not a file" in result.error


class TestCreateFile:
    """old_str 为空时创建新文件。"""

    def test_create_new_file(self, tool, tmp_path):
        new_path = tmp_path / "new_dir" / "new_file.py"
        result = tool.execute({
            "path": str(new_path),
            "old_str": "",
            "new_str": "# new file\nprint('created')\n",
        })
        assert result.success
        assert "Created new file" in result.output
        assert new_path.exists()
        assert new_path.read_text(encoding="utf-8") == "# new file\nprint('created')\n"

    def test_create_fails_if_exists(self, tool, sample_file):
        result = tool.execute({
            "path": str(sample_file),
            "old_str": "",
            "new_str": "overwrite attempt",
        })
        assert not result.success
        assert "already exists" in result.error

    def test_create_fails_if_both_empty(self, tool, tmp_path):
        result = tool.execute({
            "path": str(tmp_path / "empty.py"),
            "old_str": "",
            "new_str": "",
        })
        assert not result.success
        assert "empty" in result.error.lower()


class TestFileIntegrity:
    """确保编辑不会破坏文件其余内容。"""

    def test_large_file_integrity(self, tool, tmp_path):
        """编辑大文件时保持文件完整性。"""
        lines = [f"line_{i} = {i}" for i in range(200)]
        content = "\n".join(lines) + "\n"
        f = tmp_path / "large.py"
        f.write_text(content, encoding="utf-8")

        result = tool.execute({
            "path": str(f),
            "old_str": "line_100 = 100",
            "new_str": "line_100 = 999  # modified",
        })
        assert result.success

        new_content = f.read_text(encoding="utf-8")
        new_lines = new_content.strip().split("\n")
        assert len(new_lines) == 200
        assert "line_100 = 999  # modified" in new_content
        assert "line_0 = 0" in new_content
        assert "line_199 = 199" in new_content
