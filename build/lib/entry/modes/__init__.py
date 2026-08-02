"""entry/modes/ — mode-specific runners extracted from cli.py.

Each runner handles one execution mode (v2, chat, etc.), keeping cli.py
focused on argument parsing and dispatch.
"""

from entry.modes.v2_runner import run_v2_mode

__all__ = ["run_v2_mode"]
