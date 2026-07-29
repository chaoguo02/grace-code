from __future__ import annotations

from context.repo_map import RepoMap


def test_repo_map_skips_runtime_and_test_artifact_trees(tmp_path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text(
        "def visible_symbol():\n    return True\n",
        encoding="utf-8",
    )
    for directory in (
        ".scratch",
        ".grace",
        ".agents",
        ".codex",
        "test-results",
        "playwright-report",
    ):
        hidden = tmp_path / directory
        hidden.mkdir()
        (hidden / "large.py").write_text(
            "def hidden_symbol():\n    return False\n" * 1000,
            encoding="utf-8",
        )

    rendered = RepoMap(tmp_path).build()

    assert "src" in rendered
    assert "visible_symbol" in rendered
    assert "hidden_symbol" not in rendered
    assert ".scratch" not in rendered
