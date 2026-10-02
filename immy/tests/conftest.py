"""Shared pytest fixtures.

Isolation rules:
- `~/.immy/library.yml` is a process-wide cache used by offline mode.
  Tests that invoke the CLI `process` command will write to it via
  `offline.cache_library_info`, which would clobber the real user's
  library info. Redirect the cache to a per-session tmp dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_library_cache(tmp_path_factory, monkeypatch):
    from immy import offline as offline_mod

    isolated = tmp_path_factory.mktemp("immy-lib-cache") / "library.yml"
    monkeypatch.setattr(offline_mod, "LIBRARY_CACHE_PATH", isolated)



@pytest.fixture
def no_schema_guard(monkeypatch):
    """Opt-in for tests whose fake (MagicMock) Postgres connection can't
    answer the pre-write guard's information_schema query. Everything else
    runs the real guard — a new CLI test gets production behaviour unless it
    asks for this."""
    from immy import schema_contract

    monkeypatch.setattr(schema_contract, "assert_live_schema", lambda conn: None)
