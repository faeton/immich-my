"""Source-adapter wiring for the `photos` source (osxphotos exports).

Small, independent gaps found in the Photos Bridge review
(todo/PHOTOS-BRIDGE-REVIEW.md, "Source-adapter wiring gaps"): each one would
have let a Photos export lose to an icloudpd copy, lose its never-auto-merge
guard, or keep a corrected date only in the manifest.
"""

from __future__ import annotations

from pathlib import Path

from immy.dedup import engine, manifest


def _asset(id: int, *, source: str, path: str = "/s/IMG_0001.HEIC") -> engine.AssetLite:
    return engine.AssetLite(
        id=id, source=source, path=path, bytes=4_000_000, media_type="image",
        format="heic", width=4032, height=3024, taken_at="2025-06-01T12:00:00",
        taken_src="exif", gps_lat=None, gps_lon=None, phash=0xAAAA5555AAAA5555,
        exif_fields=40, burst_uuid=None, live_cid=None, edited=False,
    )


def test_photos_export_outranks_its_icloudpd_twin():
    assert engine.winner_score(_asset(1, source="photos")) > engine.winner_score(
        _asset(2, source="icloud")
    )


def test_library_original_still_outranks_a_photos_export():
    assert engine.winner_score(_asset(1, source="originals")) > engine.winner_score(
        _asset(2, source="photos")
    )


def test_osxphotos_edited_suffix_counts_as_edited():
    # osxphotos' default --edited-suffix is `_edited`; Takeout uses `-edited`.
    for name in ("IMG_1234_edited", "IMG_1234-edited", "IMG_1234_EDITED"):
        assert engine._EDITED_NAME_RE.search(name), name
    assert not engine._EDITED_NAME_RE.search("IMG_1234")
    assert engine.normalized_stem("/a/IMG_1234_edited.jpeg") == "img_1234"


def test_json_date_rescue_is_not_google_only(tmp_path, monkeypatch):
    """A `photos` keeper whose date came from its JSON companion gets the
    same XMP write-back a Takeout keeper does."""
    written: list[Path] = []
    monkeypatch.setattr(engine.sidecar, "write", lambda dest, patch: written.append(dest))

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    src = tmp_path / "staging" / "IMG_0001.HEIC"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 64)
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, taken_at, taken_src, media_type)"
        " VALUES (1, 'photos', ?, ?, 64, '2024-06-15T10:00:00', 'json', 'image')",
        (str(src), manifest.FINGERPRINTED),
    )
    conn.commit()

    result = engine.promote_rest(conn, originals_root=tmp_path / "originals", dry_run=False)

    assert result["sidecars_written"] == 1
    assert written == [tmp_path / "originals" / "2024" / "06" / "IMG_0001.HEIC"]
