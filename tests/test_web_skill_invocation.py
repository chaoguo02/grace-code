from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.services.agent_service import AgentService
from skills.registry import SkillRegistry


def _service_with_registry(registry: SkillRegistry):
    return SimpleNamespace(
        _registry=SimpleNamespace(_skill_registry=registry),
        _root_session_id="session-1",
        repo_path="D:/workspace",
    )


def test_web_skill_invocation_validates_and_renders_user_skill(tmp_path):
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: review\n"
        "description: Review code\n"
        "arguments: [target]\n"
        "---\n"
        "Inspect $target carefully.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(
        str(tmp_path / "skills"),
        include_builtin=False,
    )

    rendered = AgentService.resolve_user_skill(
        _service_with_registry(registry),
        "review",
        "auth.py",
    )

    assert rendered.startswith("[USER-INVOKED SKILL: review]")
    assert "Inspect auth.py carefully." in rendered


def test_web_skill_invocation_rejects_non_user_invocable_skill(tmp_path):
    skill_dir = tmp_path / "skills" / "internal"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: internal\n"
        "description: Internal only\n"
        "user-invocable: false\n"
        "---\n"
        "Do internal work.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(
        str(tmp_path / "skills"),
        include_builtin=False,
    )

    with pytest.raises(ValueError, match="not user-invocable"):
        AgentService.resolve_user_skill(
            _service_with_registry(registry),
            "internal",
        )
