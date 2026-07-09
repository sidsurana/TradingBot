"""Ensures the project root is importable so `tests.*` helpers resolve, and
isolates tests from the operator's runtime config: every Settings class reads
`.env` from the cwd, so running pytest from the repo root silently picks up
the production .env (real strategies, persistence pointed at the live
state.db) and tests fail against live state. Each test runs in a temp cwd.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
