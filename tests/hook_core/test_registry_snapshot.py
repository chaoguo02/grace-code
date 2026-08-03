"""Hook Registry — acceptance tests.

AC: Registration creates new revision.
AC: Snapshot is immune to subsequent registrations.
AC: Matcher uses CC syntax (no regex).
AC: Duplicate registration raises HookAlreadyRegisteredError.
"""

from __future__ import annotations

import pytest

from hook_core.matcher import HookMatcher, HookSelector, MatcherCompileError
from hook_core.registry import (
    HookRegistry, HookAlreadyRegisteredError, HookNotFoundError,
)


def _noop(_input) -> None:
    pass


class TestMatcher:

    def test_exact_match(self):
        m = HookMatcher("Bash")
        assert m.matches("Bash")
        assert not m.matches("BashRead")
        assert not m.matches("Read")

    def test_pipe_separated(self):
        m = HookMatcher("Edit|Write|NotebookEdit")
        assert m.matches("Edit")
        assert m.matches("Write")
        assert m.matches("NotebookEdit")
        assert not m.matches("Read")

    def test_prefix_wildcard(self):
        m = HookMatcher("mcp__github__*")
        assert m.matches("mcp__github__search")
        assert m.matches("mcp__github__push")
        assert not m.matches("Bash")
        assert not m.matches("mcp__gitlab__search")

    def test_match_all(self):
        assert HookMatcher("*").matches("anything")
        assert HookMatcher("").matches("anything")

    def test_regex_metacharacters_rejected(self):
        for bad in [".*", "foo.bar", "^Bash$", "[abc]", "foo?", "foo+"]:
            with pytest.raises(MatcherCompileError, match="regex"):
                HookMatcher(bad)

    def test_selector_all_tools(self):
        sel = HookSelector.all_tools()
        assert sel.selects("anything")

    def test_selector_filtered(self):
        sel = HookSelector.matching("Bash")
        assert sel.selects("Bash")
        assert not sel.selects("Read")

    def test_selector_multiple(self):
        sel = HookSelector.matching("Bash", "Read")
        assert sel.selects("Bash")
        assert sel.selects("Read")
        assert not sel.selects("Write")


class TestRegistry:

    def test_register_then_snapshot(self):
        reg = HookRegistry()
        reg.register("h1", "PreToolUse", _noop)
        snap = reg.snapshot()
        assert snap.revision == 1
        assert len(snap.hooks) == 1

    def test_snapshot_immune_to_later_registration(self):
        reg = HookRegistry()
        reg.register("h1", "PreToolUse", _noop)
        snap = reg.snapshot()
        reg.register("h2", "PostToolUse", _noop)

        hooks_from_snap = reg.get_hooks(snap, "PreToolUse")
        assert len(hooks_from_snap) == 1
        assert hooks_from_snap[0].name == "h1"

        hooks_current = reg.get_hooks(None, "PostToolUse")
        assert len(hooks_current) == 1
        assert hooks_current[0].name == "h2"

    def test_duplicate_name_raises(self):
        reg = HookRegistry()
        reg.register("dup", "PreToolUse", _noop)
        with pytest.raises(HookAlreadyRegisteredError):
            reg.register("dup", "PostToolUse", _noop)

    def test_unregister(self):
        reg = HookRegistry()
        reg.register("temp", "PreToolUse", _noop)
        reg.unregister("temp")
        hooks = reg.get_hooks(None, "PreToolUse")
        assert len(hooks) == 0

    def test_unregister_unknown_raises(self):
        reg = HookRegistry()
        with pytest.raises(HookNotFoundError):
            reg.unregister("does_not_exist")

    def test_get_hooks_filters_by_selector(self):
        reg = HookRegistry()
        reg.register("bash_only", "PreToolUse", _noop,
                     selector=HookSelector.matching("Bash"))
        hooks = reg.get_hooks(None, "PreToolUse", tool_name="Bash")
        assert len(hooks) == 1
        hooks2 = reg.get_hooks(None, "PreToolUse", tool_name="Read")
        assert len(hooks2) == 0

    def test_hooks_sorted_by_priority(self):
        reg = HookRegistry()
        reg.register("low", "PreToolUse", _noop, priority=200)
        reg.register("high", "PreToolUse", _noop, priority=10)
        hooks = reg.get_hooks(None, "PreToolUse")
        assert hooks[0].name == "high"
        assert hooks[1].name == "low"
