from __future__ import annotations

import warnings

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
