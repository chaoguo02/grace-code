"""runtime_core/native_child_context.py

Child context construction helpers (Phase 3 + 4).

Phase 3:
  1. _load_project_rules           — read .grace/GRACE.md (CC CLAUDE.md equivalent)
  2. resolve_child_model           — CC model parameter priority resolution

Phase 4:
  3. resolve_child_mcp_servers     — CC MCP server intersection
  4. resolve_child_permission_mode — CC permission mode inheritance
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.session.models import AgentDefinition
    from runtime_core.native_backend import NativeBackend


def _load_project_rules(project_dir: str) -> str:
    """Read .grace/GRACE.md project rules file.

    Grace Code's equivalent of CC's CLAUDE.md <system-reminder> mechanism.
    Truncated to 25KB — matches CC's auto-memory limit.
    Returns empty string if file does not exist or cannot be read.
    """
    path = Path(project_dir) / ".grace" / "GRACE.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:25_000]
    except OSError:
        return ""


def resolve_child_model(
    request_model: str,
    definition_model: str,
    parent_backend: Any,
) -> str:
    """CC model parameter priority resolution.

    Priority: request.model > definition.model > inherit parent model.

    Args:
        request_model: model from NativeChildRequest (tool call parameter)
        definition_model: model from AgentDefinition frontmatter
        parent_backend: parent NativeBackend (for inherit fallback)
    """
    if request_model:
        return request_model
    if definition_model and definition_model != "inherit":
        return definition_model
    # inherit parent
    if hasattr(parent_backend, "model_name"):
        return parent_backend.model_name
    return getattr(parent_backend, "_model", "")


# ── Phase 4: MCP server resolution ──────────────────────────────────────────


def resolve_child_mcp_servers(
    definition: "AgentDefinition",
    parent_server_names: set[str],
) -> set[str]:
    """Child MCP servers = definition.mcp_servers ∩ parent servers.

    CC behavior: child cannot expand parent's MCP permissions.
    Only servers the parent already has connected are available.
    definition.mcp_servers can be tuple[str, ...] or tuple[str|dict, ...]
    (inline definitions).  We extract server names from both forms.
    """
    requested: set[str] = set()
    for item in getattr(definition, "mcp_servers", ()) or ():
        if isinstance(item, str):
            requested.add(item)
        elif isinstance(item, dict):
            name = item.get("name", "")
            if name:
                requested.add(name)
    return requested & parent_server_names


# ── Phase 4: Permission mode inheritance ────────────────────────────────────


def resolve_child_permission_mode(
    definition: "AgentDefinition",
    parent_mode: str,
) -> str:
    """CC permission mode inheritance.

    Priority: definition.permission_mode (if set) → inherit parent_mode.
    CC permissionMode values: default | acceptEdits | dontAsk | plan | bypassPermissions.
    Empty string = not set (inherit).
    """
    mode = getattr(definition, "permission_mode", "") or ""
    stripped = mode.strip()
    if stripped:
        return stripped
    return parent_mode
