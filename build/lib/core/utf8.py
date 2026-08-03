from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_TEXT_ENCODING = "utf-8"
DEFAULT_TEXT_ERRORS = "replace"


def with_utf8_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess environment biased toward UTF-8 on all platforms.

    On Windows this prevents implicit fallback to the active code page.
    Existing caller-provided values still win.
    """
    merged = dict(os.environ)
    merged.setdefault("PYTHONUTF8", "1")
    merged.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        merged.update(env)
    return merged


def text_decode_kwargs() -> dict[str, str]:
    return {"encoding": DEFAULT_TEXT_ENCODING, "errors": DEFAULT_TEXT_ERRORS}
