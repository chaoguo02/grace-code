"""Tests for FileWriteTool path whitelist enforcement."""

from pathlib import Path

from tools.file_tool import FileWriteTool


def test_file_write_allows_whitelisted_path(tmp_path):
    target = tmp_path / "session.md"
    tool = FileWriteTool(allowed_paths=[target])

    result = tool.execute({"path": str(target), "content": "notes"})

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "notes"


def test_file_write_rejects_non_whitelisted_path(tmp_path):
    allowed = tmp_path / "session.md"
    forbidden = tmp_path / "src.py"
    tool = FileWriteTool(allowed_paths=[allowed])

    result = tool.execute({"path": str(forbidden), "content": "bad"})

    assert result.success is False
    assert "Permission denied" in (result.error or "")
    assert not forbidden.exists()
