"""P0-1: build_native_hook_settings — legacy settings.json → native hook_settings.

Verifies the reshape that production entry points (server/main.py,
entry/cli.py) use to wire real permission rules into the native
PermissionPipeline instead of falling back to builtin defaults.
"""

from __future__ import annotations

import json

from hitl.settings_loader import build_native_hook_settings


def test_empty_project_returns_empty_rules(tmp_path):
    """No settings.json → empty permission_rules (assemble falls back to builtin)."""
    hook = build_native_hook_settings(str(tmp_path))
    assert hook["permission_rules"] == {}
    assert hook["permission_mode"] == ""
    assert hook["hooks"] == {}


def _write(tmp_path, settings: dict) -> None:
    """Write settings to the project location (.grace/settings.json)."""
    target = tmp_path / ".grace" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings), encoding="utf-8")


def test_legacy_arrays_reshape_to_tier_dict(tmp_path):
    """permissions.deny/ask/allow arrays → permission_rules {pattern: tier}."""
    _write(tmp_path, {
        "permissions": {
            "deny": ["Bash rm -rf *"],
            "ask": ["Write", "Edit"],
            "allow": ["Read", "Grep"],
        },
    })
    hook = build_native_hook_settings(str(tmp_path))
    assert hook["permission_rules"] == {
        "Bash rm -rf *": "deny",
        "Write": "ask",
        "Edit": "ask",
        "Read": "allow",
        "Grep": "allow",
    }


def test_permission_mode_passthrough(tmp_path):
    """permission_mode is copied verbatim to the native dict."""
    _write(tmp_path, {"permissions": {}, "permission_mode": "acceptEdits"})
    hook = build_native_hook_settings(str(tmp_path))
    assert hook["permission_mode"] == "acceptEdits"


def test_malformed_settings_falls_back_empty(tmp_path):
    """Corrupt JSON → no crash, empty rules (builtin defaults take over)."""
    _write(tmp_path, {"bad json": ""})  # replaced below with malformed text
    target = tmp_path / ".grace" / "settings.json"
    target.write_text("{ not valid json ", encoding="utf-8")
    hook = build_native_hook_settings(str(tmp_path))
    assert hook["permission_rules"] == {}
    assert hook["permission_mode"] == ""
