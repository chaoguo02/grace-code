from __future__ import annotations

import os
import warnings

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_forge_runtime_state(tmp_path_factory):
    """Keep framework-private state out of both the checkout and user home."""
    key = "FORGE_AGENT_STATE_DIR"
    previous = os.environ.get(key)
    os.environ[key] = str(tmp_path_factory.getbasetemp() / "forge-agent-state")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

try:
    from requests import RequestsDependencyWarning

    warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
except Exception:  # pragma: no cover - defensive for environments without requests
    pass

try:
    from requests.exceptions import RequestsDependencyWarning as ExceptionsRequestsDependencyWarning

    warnings.filterwarnings("ignore", category=ExceptionsRequestsDependencyWarning)
except Exception:  # pragma: no cover - defensive for requests variants
    pass
