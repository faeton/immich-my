"""`photos` source adapter (dedup/photos.py) against a fixture batch shaped
like osxphotos 0.77.1's JSON export report (`cli/report_writer.py`,
`prepare_export_results_for_writing`): a list of per-file records whose
`filename` is the path on the Mac and whose `uuid` names the Photos asset.
Re-verify against a real export once `osxphotos` runs on m3max (Phase 0)."""

from __future__ import annotations

import json
from pathlib import Path

from immy.dedup import photos

MAC_ROOT = "/Users/ivan/Pictures/immy-export/batch-0042"


def _record(rel: str, uuid: str, **kw) -> dict:
    base = {
        "datetime": "2026-09-20T10:00:00", "filename": f"{MAC_ROOT}/{rel}",
        "exported": True, "new": True, "updated": False, "skipped": False,
        "missing": False, "error": "", "sidecar_json": True, "uuid": uuid,
    }
    return {**base, **kw}


def _batch(tmp_path: Path, files: dict[str, str | None], extra_records=()) -> Path:
    root = tmp_path / "ready" / "batch-0042"
    records = list(extra_records)
    for rel, uuid in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        if uuid:
            records.append(_record(rel, uuid))
    (root / photos.REPORT_NAME).write_text(json.dumps(records, indent=4))
    return root


def test_uuid_and_component_for_a_live_photo_raw_and_edit(tmp_path):
    root = _batch(tmp_path, {
        "2026/07/IMG_1742.HEIC": "UUID-LIVE",
        "2026/07/IMG_1742.mov": "UUID-LIVE",
        "2026/07/IMG_1743.DNG": "UUID-RAW",
        "2026/07/IMG_1743.JPG": "UUID-RAW",
        "2026/07/IMG_1744_edited.jpeg": "UUID-EDIT",
        "2026/07/IMG_1745.MOV": "UUID-VIDEO",          # a plain video, no still
    })
    got = {
        rel: (photos.uuid_for(root / rel), photos.component_for(root / rel, photos.uuid_for(root / rel)))
        for rel in (
            "2026/07/IMG_1742.HEIC", "2026/07/IMG_1742.mov", "2026/07/IMG_1743.DNG",
            "2026/07/IMG_1743.JPG", "2026/07/IMG_1744_edited.jpeg", "2026/07/IMG_1745.MOV",
        )
    }
    assert got == {
        "2026/07/IMG_1742.HEIC": ("UUID-LIVE", "original"),
        "2026/07/IMG_1742.mov": ("UUID-LIVE", "live_video"),
        "2026/07/IMG_1743.DNG": ("UUID-RAW", "raw"),
        "2026/07/IMG_1743.JPG": ("UUID-RAW", "original"),
        "2026/07/IMG_1744_edited.jpeg": ("UUID-EDIT", "edited"),
        "2026/07/IMG_1745.MOV": ("UUID-VIDEO", "original"),
    }


def test_same_basename_in_two_folders_matches_by_relative_path(tmp_path):
    root = _batch(tmp_path, {"2019/05/IMG_1.JPG": "U-2019", "2026/07/IMG_1.JPG": "U-2026"})
    assert photos.uuid_for(root / "2026/07/IMG_1.JPG") == "U-2026"
    assert photos.uuid_for(root / "2019/05/IMG_1.JPG") == "U-2019"


def test_errored_or_missing_records_and_unlisted_files_have_no_uuid(tmp_path):
    root = _batch(
        tmp_path, {"a/IMG_1.JPG": None, "a/IMG_2.JPG": None},
        extra_records=[
            _record("a/IMG_1.JPG", "U-1", error="download failed"),
            _record("a/IMG_2.JPG", "U-2", missing=True),
        ],
    )
    assert photos.uuid_for(root / "a/IMG_1.JPG") is None
    assert photos.uuid_for(root / "a/IMG_2.JPG") is None


def test_no_report_means_no_identity(tmp_path):
    f = tmp_path / "IMG_1.JPG"
    f.write_bytes(b"x")
    assert photos.companion_fields(f, {"taken_at": None}) == {"taken_at": None}


def test_sidecar_date_and_location_override_exif(tmp_path):
    """A date/location edited in Photos lives only in the library; the
    exiftool-format JSON sidecar carries it, and it must win."""
    root = _batch(tmp_path, {"2026/07/IMG_1.HEIC": "U-1"})
    media = root / "2026/07/IMG_1.HEIC"
    media.with_name(media.name + ".json").write_text(json.dumps([{
        "SourceFile": "IMG_1.HEIC",
        "EXIF:DateTimeOriginal": "2026:07:12 18:54:23",
        "EXIF:GPSLatitude": 38.72, "EXIF:GPSLatitudeRef": "N",
        "EXIF:GPSLongitude": 9.14, "EXIF:GPSLongitudeRef": "W",
    }]))
    out = photos.companion_fields(media, {
        "taken_at": "2026-07-12T17:54:23", "taken_src": "exif",
        "gps_lat": None, "gps_lon": None,
    })
    assert out["taken_at"] == "2026-07-12T18:54:23" and out["taken_src"] == "json"
    assert (out["gps_lat"], out["gps_lon"]) == (38.72, -9.14)
    assert (out["source_uid"], out["component"]) == ("U-1", "original")


def test_sidecar_agreeing_with_exif_changes_nothing(tmp_path):
    root = _batch(tmp_path, {"IMG_1.HEIC": "U-1"})
    media = root / "IMG_1.HEIC"
    media.with_name(media.name + ".json").write_text(json.dumps([{
        "EXIF:DateTimeOriginal": "2026:07:12 18:54:23",
    }]))
    fields = {"taken_at": "2026-07-12T18:54:23", "taken_src": "exif", "gps_lat": None, "gps_lon": None}
    out = photos.companion_fields(media, fields)
    assert out["taken_src"] == "exif"


def test_register_and_fingerprint_a_photos_batch(tmp_path):
    import numpy as np
    import pyvips

    from immy.dedup import engine, manifest

    root = _batch(tmp_path, {"2026/07/IMG_1.JPG": "U-1", "2026/07/IMG_1.mov": "U-1"})
    pixels = np.random.default_rng(1).integers(0, 255, size=(64, 64), dtype=np.uint8)
    pyvips.Image.new_from_memory(pixels.tobytes(), 64, 64, 1, "uchar").jpegsave(
        str(root / "2026/07/IMG_1.JPG"), Q=90,
    )
    # Parseable media bytes for the video half (the fixture's b"x" is a stub);
    # only the adapter's naming logic is under test here.
    pyvips.Image.new_from_memory(pixels[::-1].tobytes(), 64, 64, 1, "uchar").jpegsave(
        str(root / "2026/07/IMG_1.mov"), Q=90,
    )
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    assert manifest.register(conn, "photos", root).new == 2       # report JSON is not media
    engine.fingerprint_pending(conn)
    rows = conn.execute(
        "SELECT format, status, source_uid, component, sha256 IS NOT NULL FROM asset ORDER BY format"
    ).fetchall()
    assert rows == [
        ("jpg", manifest.FINGERPRINTED, "U-1", "original", 1),
        ("mov", manifest.FINGERPRINTED, "U-1", "live_video", 1),
    ]
