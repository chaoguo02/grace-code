"""entry/bootstrap/ — system assembly functions.

Each module handles one aspect of application bootstrap:
  memory_bootstrap  — memory_store + memory_context + external_store
  hook_bootstrap    — HookDispatcher assembly
  registry_factory  — ToolRegistry assembly with all built-in tools

Constitution: entry/ is the user entry point. Assembly logic belongs here
(not in cli.py). cli.py should only parse arguments, dispatch modes, and print.
"""

from entry.bootstrap.memory_bootstrap import init_memory
from entry.bootstrap.hook_bootstrap import init_hook_dispatcher
from entry.bootstrap.registry_factory import build_registry

__all__ = ["init_memory", "init_hook_dispatcher", "build_registry"]
