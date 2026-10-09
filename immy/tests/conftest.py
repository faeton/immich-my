"""Shared pytest fixtures.

Isolation rules:
- `~/.immy/library.yml` is a process-wide cache used by offline mode.
  Tests that invoke the CLI `process` command will write to it via
  `offline.cache_library_info`, which would clobber the real user's
  library info. Redirect the cache to a per-session tmp dir.
"""

from __future__ import annotations


import socket

import pytest

# Ports of the live Immich stack (Postgres in-container / Postgres tunnelled
# to the host / Immich API). No test may ever open a connection to them.
_LIVE_PORTS = frozenset({5432, 15432, 2283})


class LiveServiceAccess(BaseException):
    """Raised when a test tries to reach a live Immich/Postgres port.

    A BaseException on purpose: production code fails open on connect errors
    (`except Exception`), which would otherwise swallow the guard silently."""


def _port_of(address) -> int | None:
    if isinstance(address, tuple) and len(address) >= 2:
        try:
            return int(address[1])
        except (TypeError, ValueError):
            return None
    return None


_SCRATCH_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SCRATCH_KEYS = frozenset({"host", "port", "user", "password", "dbname"})


def _scratch_dsn_port(dsn: str) -> int | None:
    """The port of a usable scratch DSN, or None to refuse it.

    Read through libpq's own parser, since a URL's query string can
    override its authority (`…:55432/db?port=5432`). Usable: a loopback
    host, one explicit non-live port, and nothing else that could redirect
    the connection (hostaddr, service, a host list)."""
    try:
        from psycopg.conninfo import conninfo_to_dict
        params = conninfo_to_dict(dsn)
    except Exception:
        return None
    if set(params) - _SCRATCH_KEYS or params.get("host") not in _SCRATCH_HOSTS:
        return None
    try:
        port = int(str(params.get("port", "")))
    except ValueError:
        return None
    return None if port in _LIVE_PORTS else port


@pytest.fixture(autouse=True)
def _block_live_services(monkeypatch, request):
    """Hermeticity guard: any socket connect to a live Immich/Postgres port,
    and any real `psycopg.connect` aimed at one (libpq opens its own socket,
    which Python-level socket patching cannot see), raises. Tests that need a
    DB must stub `immy.pg.connect` / pass a fake connection.

    One narrow exception: a test marked `scratch_pg` may connect to exactly
    the throwaway database in `IMMY_TEST_PG_DSN` (scripts/test-pg.sh), and
    only if that DSN names an explicit port that isn't a live one."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if _port_of(address) in _LIVE_PORTS:
            raise LiveServiceAccess(f"test tried to connect to live port {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if _port_of(address) in _LIVE_PORTS:
            raise LiveServiceAccess(f"test tried to connect to live port {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return

    import os
    scratch = os.environ.get("IMMY_TEST_PG_DSN", "")
    scratch_port = _scratch_dsn_port(scratch) if scratch else None
    allow_scratch = (request.node.get_closest_marker("scratch_pg") is not None
                     and scratch_port is not None)
    real_pg_connect = psycopg.connect

    def guarded_pg_connect(*args, **kwargs):
        conninfo = args[0] if args else kwargs.get("conninfo", "")
        # Keyword arguments override the DSN in libpq; only harmless ones pass.
        if allow_scratch and conninfo == scratch and set(kwargs) <= {"autocommit", "conninfo"}:
            return real_pg_connect(*args, **kwargs)
        # Every port libpq would default to (none given → 5432) is live.
        port = kwargs.get("port")
        conninfo = args[0] if args else kwargs.get("conninfo", "")
        if port is None and conninfo and "port=" in str(conninfo):
            port = str(conninfo).split("port=", 1)[1].split()[0]
        try:
            port_num = 5432 if port in (None, "") else int(port)
        except (TypeError, ValueError):
            port_num = None
        if port_num in _LIVE_PORTS:
            raise LiveServiceAccess(
                f"test tried psycopg.connect to live port {port_num} "
                "(stub immy.pg.connect instead)"
            )
        raise LiveServiceAccess("test tried a real psycopg.connect (stub it)")

    monkeypatch.setattr(psycopg, "connect", guarded_pg_connect)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(
        lambda cls, *a, **kw: guarded_pg_connect(*a, **kw)))


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
