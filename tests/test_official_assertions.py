# PROMPT: "When HackerEarth provides the official assertions.py (10 example test
#          assertions the API must pass), it should run automatically alongside
#          our suite with no code changes. Until then it must be skipped cleanly.
#          Write a pytest shim that locates assertions.py at the repo root, and
#          if present, executes its assertion functions / module body against the
#          running app, skipping with a clear reason when the file is absent."
# CHANGES MADE: Made discovery tolerant of either a module that self-executes on
#          import or one exposing test_/assert_ functions; injected a TestClient
#          and base_url so common harness shapes work; kept it fully skipped (not
#          failed) when the official file hasn't been dropped in yet.

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSERTIONS_PATH = REPO_ROOT / "assertions.py"


def _load_official_module():
    spec = importlib.util.spec_from_file_location("official_assertions", ASSERTIONS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.mark.skipif(
    not ASSERTIONS_PATH.exists(),
    reason="Official assertions.py not provided yet — drop it at the repo root to enable.",
)
def test_official_assertions(client):
    """Run the official assertions against the live in-process API.

    The official file's exact shape is unknown, so we support the common ones:
      * module-level code that runs on import (we pass if import succeeds)
      * test_*/assert_* functions that take (client) or (base_url) or no args
    """
    module = _load_official_module()

    callables = [
        obj
        for name, obj in vars(module).items()
        if callable(obj) and (name.startswith("test_") or name.startswith("assert_"))
    ]

    if not callables:
        # Self-executing harness: importing it without error counts as passing.
        return

    base_url = "http://testserver"
    for fn in callables:
        params = inspect.signature(fn).parameters
        if "client" in params:
            fn(client)
        elif "base_url" in params or "url" in params:
            fn(base_url)
        else:
            fn()
