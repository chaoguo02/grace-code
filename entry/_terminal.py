"""Shared ANSI terminal helpers — single definition point.

Claude Code pattern: centralized token-based theme system with ~40 semantic tokens.
Our approach: simpler color helpers with TTY detection, consumed by all entry modules.
"""

from __future__ import annotations

import sys

_IS_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _IS_TTY else text


# ── Foreground colors ──
green   = lambda t: _c(t, "32")
yellow  = lambda t: _c(t, "33")
red     = lambda t: _c(t, "31")
cyan    = lambda t: _c(t, "36")
magenta = lambda t: _c(t, "35")

# ── Formatting ──
bold = lambda t: _c(t, "1")
dim  = lambda t: _c(t, "2")

# ── Backgrounds ──
bg_yellow = lambda t: _c(t, "43;30")
bg_red    = lambda t: _c(t, "41;37")

# ── Cursor ──
def _move_up(n: int) -> str:
    return f"\033[{n}A" if n > 0 else ""

def _clear_line() -> str:
    return "\033[2K"
