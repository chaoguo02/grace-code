"""Single shared utility for YAML frontmatter parsing.

Claude Code pattern: separate schemas per file type, but a single parser utility.
This module handles the mechanical "---" delimiter split; each caller is
responsible for schema-specific yaml.safe_load() and validation.
"""

from __future__ import annotations

import re
from typing import Any

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split YAML frontmatter from a Markdown file.

    Returns (frontmatter_text, body_text). If no frontmatter is found,
    returns ("", text).
    """
    m = _FM_RE.match(text)
    if not m:
        return "", text.strip()
    fm = m.group(1).strip()
    body = m.group(2).strip()
    return fm, body


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split frontmatter AND parse YAML in one call.

    Returns (frontmatter_dict, body_text).
    """
    import yaml

    fm_text, body = split_frontmatter(text)
    if not fm_text:
        return {}, body
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body
