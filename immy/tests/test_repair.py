"""Unit tests for in-place thumbnail repair — the source-resolution logic
(mapping Immich originalPath back to the Mac trip file, and flagging NAS
orphans that have no local source)."""

from __future__ import annotations

from pathlib import Path

from immy.repair import _resolve_source


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
