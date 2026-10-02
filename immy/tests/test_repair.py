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
