"""Unit tests for in-place thumbnail repair — the source-resolution logic
(mapping Immich originalPath back to the Mac trip file, and flagging NAS
orphans that have no local source)."""

from __future__ import annotations

from pathlib import Path

from unittest.mock import MagicMock

from immy.pg import LibraryInfo
from immy.repair import _resolve_source, find_broken


def test_resolve_maps_originalpath_to_mac_file(tmp_path):
    trip = tmp_path / "2024-09-foo"
    trip.mkdir()
    (trip / "IMG_1.insp").write_bytes(b"x")
    src = _resolve_source(
        "/mnt/external/originals/2024-09-foo/IMG_1.insp",
        "/mnt/external/originals", trip,
    )
    assert src == trip / "IMG_1.insp"


def test_resolve_handles_nested_subdir(tmp_path):
    trip = tmp_path / "2024-09-foo"
    (trip / "sub").mkdir(parents=True)
    (trip / "sub" / "a.jpg").write_bytes(b"x")
    src = _resolve_source(
        "/mnt/external/originals/2024-09-foo/sub/a.jpg",
        "/mnt/external/originals/", trip,  # trailing slash on root tolerated
    )
    assert src == trip / "sub" / "a.jpg"


def test_resolve_orphan_returns_none(tmp_path):
    # Immich has a .dng the Mac doesn't (Insta360 .insp is the truth) → orphan.
    trip = tmp_path / "2024-09-foo"
    trip.mkdir()
    src = _resolve_source(
        "/mnt/external/originals/2024-09-foo/IMG_1.dng",
        "/mnt/external/originals", trip,
    )
    assert src is None


def test_resolve_other_trip_prefix_returns_none(tmp_path):
    trip = tmp_path / "2024-09-foo"
    trip.mkdir()
    (trip / "x.jpg").write_bytes(b"x")
    # originalPath belongs to a different trip folder → not ours.
    src = _resolve_source(
        "/mnt/external/originals/2024-10-bar/x.jpg",
        "/mnt/external/originals", trip,
    )
    assert src is None


def test_find_broken_escapes_trip_and_scopes_to_library():
    """`_`/`%` in a trip name are LIKE wildcards — escaped so a sibling trip
    never matches — and NULL-libraryId rows are out of scope."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    lib = LibraryInfo(id="lib-1", owner_id="o", container_root="/mnt/external/originals")

    find_broken(conn, "lib-1", lib, "2024_06-trip")

    sql, params = cur.execute.call_args.args
    assert "ESCAPE" in sql
    assert '"libraryId" IS NULL' not in sql
    assert params["prefix"] == "/mnt/external/originals/2024\\_06-trip/%"
    assert params["lib"] == "lib-1"


# --- exit codes (Task 8) ---------------------------------------------------

import pytest
import yaml
from typer.testing import CliRunner

from immy import repair as repair_mod
from immy.cli import app
from immy.config import Config, MediaConfig, ImmichConfig, PgConfig


@pytest.fixture
def repair_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({
        "originals_root": str(tmp_path / "originals"),
        "immich": {"url": "http://fake", "api_key": "k", "library_id": "lib-1"},
        "pg": {"host": "h", "port": 5432, "user": "u", "password": "p",
               "database": "immich"},
        "media": {"host_root": "/nas/media", "container_root": "/data"},
    }))
    monkeypatch.setenv("IMMY_CONFIG", str(cfg))
    return cfg


def test_repair_thumbs_exits_1_on_trip_error_but_runs_every_trip(
    repair_config, tmp_path, monkeypatch,
):
    trips = []
    for name in ("trip-a", "trip-b"):
        (tmp_path / name).mkdir()
        trips.append(tmp_path / name)
    seen = []

    def _repair(folder, config, **kw):
        seen.append(folder.name)
        if folder.name == "trip-a":
            raise RuntimeError("pg connect failed")
        return repair_mod.TripRepair(trip=folder.name)

    monkeypatch.setattr(repair_mod, "repair_trip", _repair)
    result = CliRunner().invoke(app, ["repair-thumbs", *map(str, trips)])
    assert result.exit_code == 1, result.stdout
    assert seen == ["trip-a", "trip-b"]
    assert "pg connect failed" in result.stdout


def test_repair_thumbs_exits_1_on_missing_folder(repair_config, tmp_path, monkeypatch):
    monkeypatch.setattr(repair_mod, "repair_trip",
                        lambda f, c, **kw: repair_mod.TripRepair(trip=f.name))
    result = CliRunner().invoke(app, ["repair-thumbs", str(tmp_path / "nope")])
    assert result.exit_code == 1, result.stdout


def test_repair_thumbs_exits_0_when_clean(repair_config, tmp_path, monkeypatch):
    (tmp_path / "trip-a").mkdir()
    monkeypatch.setattr(repair_mod, "repair_trip",
                        lambda f, c, **kw: repair_mod.TripRepair(trip=f.name))
    result = CliRunner().invoke(app, ["repair-thumbs", str(tmp_path / "trip-a")])
    assert result.exit_code == 0, result.stdout


def test_repair_trip_partial_generation_errors_are_an_error(tmp_path, monkeypatch):
    """Some assets regenerate, one blows up: the good rows still upsert, but
    the trip reports `error` (it used to stay `ok`, hiding the failure)."""
    trip = tmp_path / "trip"
    trip.mkdir()
    (trip / "a.jpg").write_bytes(b"x")
    (trip / "b.jpg").write_bytes(b"x")
    cfg = Config(
        originals_root=None,
        immich=ImmichConfig(url="u", api_key="k", library_id="lib-1"),
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d"),
        media=MediaConfig(host_root="/nas", container_root="/data"),
        ml=None, notes_filename=None, source=None,
    )
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    monkeypatch.setattr(repair_mod.pg_mod, "connect", lambda c: conn)
    monkeypatch.setattr(
        repair_mod.pg_mod, "fetch_library_info",
        lambda c, lid: LibraryInfo(id=lid, owner_id="o", container_root="/orig"))
    monkeypatch.setattr(repair_mod, "find_broken", lambda *a: [
        ("id-a", "/orig/trip/a.jpg", "IMAGE"), ("id-b", "/orig/trip/b.jpg", "IMAGE")])

    class _DF:
        kind = "thumbnail"
        relative_path = "thumbs/a.webp"
        is_progressive = False
        is_transparent = False

    class _Res:
        files = [_DF()]

    def _compute(*, source_media, **kw):
        if source_media.name == "b.jpg":
            raise RuntimeError("pyvips exploded")
        return _Res()

    monkeypatch.setattr(repair_mod, "compute_for_asset", _compute)
    monkeypatch.setattr(repair_mod, "_push_files", lambda *a: None)

    res = repair_mod.repair_trip(trip, cfg)
    assert res.rows_upserted == 1
    assert res.status == "error"
    assert "pyvips exploded" in res.detail
