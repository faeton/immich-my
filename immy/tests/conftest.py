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


@pytest.fixture(autouse=True)
def _stub_live_schema_guard(request, monkeypatch):
    """The CLI's pre-write schema guard (`schema_contract.assert_live_schema`)
    queries information_schema; the suite's MagicMock connections can't
    answer it. Stub it out except in tests marked `real_schema_guard`, and
    tests that monkeypatch it themselves still win (their patch lands
    after this one)."""
    if request.node.get_closest_marker("real_schema_guard"):
        return
    from immy import schema_contract

    monkeypatch.setattr(schema_contract, "assert_live_schema", lambda conn: None)
