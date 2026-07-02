"""Tests for lightweight memory selector header parsing."""

from memory.selector import _parse_header


def test_parse_header_prefers_top_level_type_when_metadata_exists():
    text = """---
name: stale-reference
description: External docs pointer
type: reference
metadata:
  stale: true
---
body ignored
"""

    header = _parse_header("stale-reference", text, 123.0)

    assert header is not None
    assert header.type == "reference"


def test_parse_header_falls_back_to_legacy_metadata_type():
    text = """---
name: old-rule
description: Old procedural memory
metadata:
  type: procedural
---
body ignored
"""

    header = _parse_header("old-rule", text, 123.0)

    assert header is not None
    assert header.type == "feedback"
