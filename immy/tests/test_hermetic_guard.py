"""The conftest live-service guard must stop any test reaching Immich/PG."""

from __future__ import annotations

import socket

import psycopg
import os

import pytest

from conftest import LiveServiceAccess
from immy import pg as pg_mod
from immy.config import PgConfig


@pytest.mark.parametrize("port", [5432, 15432, 2283])
def test_socket_connect_to_live_port_raises(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(LiveServiceAccess):
            s.connect(("127.0.0.1", port))
    finally:
        s.close()


def test_pg_connect_to_live_port_raises_through_fail_open_handlers():
    cfg = PgConfig(host="127.0.0.1", port=15432, user="postgres",
                   password="x", database="immich")
    with pytest.raises(LiveServiceAccess):
        try:
            pg_mod.connect(cfg)
        except Exception:  # production code fails open like this
            pytest.fail("guard must not be swallowable by `except Exception`")


def test_psycopg_connect_default_port_raises():
    with pytest.raises(LiveServiceAccess):
        psycopg.connect("host=127.0.0.1 dbname=immich")


def test_unmarked_test_cannot_reach_the_scratch_db_either():
    """The scratch_pg opt-in is per test: without the marker, even the
    throwaway DSN is refused."""
    import psycopg
    from conftest import LiveServiceAccess
    with pytest.raises(LiveServiceAccess):
        psycopg.connect("postgresql://postgres:test@127.0.0.1:55432/postgres")


def test_scratch_dsn_port_parsing():
    from conftest import _scratch_dsn_port
    assert _scratch_dsn_port("postgresql://u:p@127.0.0.1:55432/db") == 55432
    assert _scratch_dsn_port("postgresql://u:p@127.0.0.1/db") is None   # implicit 5432 → refused
    assert _scratch_dsn_port("host=localhost port=55432 dbname=x") == 55432


@pytest.mark.parametrize("dsn", [
    "postgresql://u@127.0.0.1:55432/db?port=5432",          # query overrides the authority
    "postgresql://u@127.0.0.1:55432/db?port=15432",
    "postgresql://u@127.0.0.1:55432/db?host=n5",
    "postgresql://u@127.0.0.1:55432/db?hostaddr=10.0.0.5",
    "postgresql://u@127.0.0.1:55432/db?service=live",
    "postgresql://u@127.0.0.1:55432,127.0.0.1:5432/db",     # a host list
    "postgresql://u@n5:55432/db",                            # not loopback
    "host=127.0.0.1 port=55432 port=5432",
    "not a dsn ===",
])
def test_scratch_dsn_refuses_anything_that_could_redirect(dsn):
    from conftest import _scratch_dsn_port
    assert _scratch_dsn_port(dsn) is None


@pytest.mark.scratch_pg
def test_scratch_marker_still_refuses_keyword_overrides(monkeypatch):
    """Even a marked test can't redirect the scratch DSN with keywords."""
    import psycopg
    from conftest import LiveServiceAccess
    dsn = "postgresql://postgres:test@127.0.0.1:55432/postgres"
    if os.environ.get("IMMY_TEST_PG_DSN") != dsn:
        pytest.skip("needs IMMY_TEST_PG_DSN set to the default scratch DSN")
    with pytest.raises(LiveServiceAccess):
        psycopg.connect(dsn, port=5432)
