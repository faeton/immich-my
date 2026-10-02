"""Capture-time and sidecar-precedence correctness for `immy process`.

Immich 3.0.2 convention (verified read-only against the live DB):
`asset.localDateTime` is the wall clock at capture stored as if UTC
(naive local + `+00:00`); `asset.fileCreatedAt` and
`asset_exif.dateTimeOriginal` are the true UTC instant. Values in a
separate `.xmp` SIDECAR (written by immy's rules or the user) beat the
embedded tags for DateTimeOriginal, GPS and timezone; XMP embedded in the
media file is NOT elevated over embedded EXIF.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from immy import process as process_mod
from immy.exif import ExifRow, read_folder
from immy.paths import resolve_writable_paths
from immy.pg import LibraryInfo


FIXTURES = Path(__file__).parent / "fixtures"
LIB = LibraryInfo(id="lib-1", owner_id="owner-1", container_root="/data")
UTC = timezone.utc


def _build(tmp_path: Path, raw: dict, sidecar: dict | None = None):
    trip = tmp_path / "trip"
    trip.mkdir(exist_ok=True)
    media = trip / "IMG_0001.JPG"
    media.write_bytes(b"x")
    row = ExifRow(path=media, raw=raw, sidecar=sidecar or {})
    return process_mod.build_rows(media, trip, row, LIB)


# --- localDateTime is wall clock, dateTimeOriginal is the instant ----------


def test_exif_offset_gives_wall_local_and_utc_instant(tmp_path: Path):
    asset, exif = _build(tmp_path, {
        "EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
        "EXIF:OffsetTimeOriginal": "+02:00",
    })
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert asset.file_created_at == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    assert exif.time_zone == "UTC+2"


def test_xmp_inline_offset_gives_wall_local(tmp_path: Path):
    asset, exif = _build(tmp_path, {
        "XMP:DateTimeOriginal": "2025:07:01 12:00:00+02:00",
    })
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    assert exif.time_zone == "UTC+2"


def test_quicktime_create_date_is_utc_and_localised_by_zone(tmp_path: Path):
    asset, exif = _build(tmp_path, {
        "QuickTime:CreateDate": "2025:07:01 10:00:00",
        "QuickTime:TimeZone": "+02:00",
    })
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)


def test_no_offset_known_keeps_naive_as_utc(tmp_path: Path):
    asset, exif = _build(tmp_path, {"EXIF:DateTimeOriginal": "2025:07:01 12:00:00"})
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert asset.file_created_at == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert exif.date_time_original == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert exif.time_zone is None


def test_quicktime_without_zone_unchanged(tmp_path: Path):
    asset, exif = _build(tmp_path, {"QuickTime:CreateDate": "2025:07:01 10:00:00"})
    assert asset.local_date_time == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)


def test_iana_zone_passthrough(tmp_path: Path):
    asset, exif = _build(tmp_path, {
        "QuickTime:CreateDate": "2025:07:01 10:00:00",
        "QuickTime:TimeZone": "Europe/Kyiv",
    })
    assert exif.time_zone == "Europe/Kyiv"
    assert asset.local_date_time == datetime(2025, 7, 1, 13, 0, tzinfo=UTC)


def test_dateless_falls_back_to_mtime(tmp_path: Path):
    asset, exif = _build(tmp_path, {})
    assert exif.date_time_original is None
    assert asset.local_date_time == asset.file_created_at == asset.file_modified_at


# --- sidecar precedence ---------------------------------------------------


def test_sidecar_date_beats_embedded_exif(tmp_path: Path):
    asset, exif = _build(
        tmp_path,
        {"EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
         "XMP:DateTimeOriginal": "2025:07:01 12:00:00+02:00"},
        sidecar={"XMP:DateTimeOriginal": "2025:07:01 12:00:00+02:00"},
    )
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)


def test_naive_sidecar_date_keeps_embedded_offset(tmp_path: Path):
    # clock-drift fix: sidecar carries the corrected wall clock, the camera's
    # own offset still applies.
    asset, exif = _build(
        tmp_path,
        {"EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
         "EXIF:OffsetTimeOriginal": "+02:00"},
        sidecar={"XMP:DateTimeOriginal": "2025:07:01 13:30:00"},
    )
    assert asset.local_date_time == datetime(2025, 7, 1, 13, 30, tzinfo=UTC)
    assert exif.date_time_original == datetime(2025, 7, 1, 11, 30, tzinfo=UTC)


def test_embedded_xmp_does_not_beat_embedded_exif(tmp_path: Path):
    _, exif = _build(tmp_path, {
        "EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
        "EXIF:OffsetTimeOriginal": "+02:00",
        "XMP:DateTimeOriginal": "2020:01:01 00:00:00+05:00",
    })
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)


def test_sidecar_offset_beats_embedded_timezone(tmp_path: Path):
    asset, exif = _build(
        tmp_path,
        {"EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
         "EXIF:OffsetTimeOriginal": "+00:00"},
        sidecar={"XMP:DateTimeOriginal": "2025:07:01 12:00:00+04:00"},
    )
    assert exif.time_zone == "UTC+4"
    assert exif.date_time_original == datetime(2025, 7, 1, 8, 0, tzinfo=UTC)
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)


def test_sidecar_gps_beats_embedded(tmp_path: Path):
    _, exif = _build(
        tmp_path,
        {"Composite:GPSLatitude": 1.0, "Composite:GPSLongitude": 2.0,
         "EXIF:GPSLatitude": 1.0, "EXIF:GPSLongitude": 2.0},
        sidecar={"XMP:GPSLatitude": -20.3, "XMP:GPSLongitude": 57.4},
    )
    assert (exif.latitude, exif.longitude) == (-20.3, 57.4)


def test_embedded_gps_used_without_sidecar(tmp_path: Path):
    _, exif = _build(tmp_path, {
        "Composite:GPSLatitude": 1.0, "Composite:GPSLongitude": 2.0,
        "XMP:GPSLatitude": 9.0, "XMP:GPSLongitude": 9.0,
    })
    assert (exif.latitude, exif.longitude) == (1.0, 2.0)


# --- read_folder: sidecar namespace + NAS sidecars_root --------------------


def _jpeg_with_exif_date(dst: Path, when: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes((FIXTURES / "trip-anchor-simple" / "IMG_A.JPG").read_bytes())
    subprocess.run(
        ["exiftool", "-overwrite_original", f"-EXIF:DateTimeOriginal={when}", str(dst)],
        check=True, capture_output=True,
    )


def _write_xmp(path: Path, when: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["exiftool", "-q", "-q", f"-XMP:DateTimeOriginal={when}", str(path)],
        check=True, capture_output=True,
    )


def test_read_folder_keeps_sidecar_keys_separate(tmp_path: Path):
    trip = tmp_path / "trip"
    media = trip / "IMG_0001.JPG"
    _jpeg_with_exif_date(media, "2025:07:01 12:00:00")
    _write_xmp(trip / "IMG_0001.xmp", "2025:07:01 12:00:00+02:00")

    [row] = read_folder(trip)
    assert row.sidecar["XMP:DateTimeOriginal"] == "2025:07:01 12:00:00+02:00"
    # Still merged into raw for the audit rules (historical behaviour).
    assert row.raw["XMP:DateTimeOriginal"] == "2025:07:01 12:00:00+02:00"
    _, exif = process_mod.build_rows(media, trip, row, LIB)
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, tzinfo=UTC)


def test_read_folder_finds_sidecar_under_sidecars_root(tmp_path: Path):
    originals = tmp_path / "originals"
    trip = originals / "2025-trip"
    media = trip / "sub" / "IMG_0001.JPG"
    _jpeg_with_exif_date(media, "2025:07:01 12:00:00")
    paths = resolve_writable_paths(
        trip, originals_root=originals, sidecars_root=tmp_path / "sidecars",
    )
    side = paths.xmp_path(media)
    assert side == tmp_path / "sidecars" / "2025-trip" / "sub" / "IMG_0001.xmp"
    _write_xmp(side, "2025:07:01 12:00:00+02:00")

    [row] = read_folder(trip, paths=paths)
    assert row.sidecar["XMP:DateTimeOriginal"] == "2025:07:01 12:00:00+02:00"
    # Without the NAS paths the mirror sidecar is invisible (Mac layout).
    [mac_row] = read_folder(trip)
    assert mac_row.sidecar == {}


def test_process_trip_reads_sidecars_through_paths(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_read_folder(folder, *, paths=None):
        seen["paths"] = paths
        return []

    monkeypatch.setattr(process_mod, "read_folder", fake_read_folder)
    trip = tmp_path / "trip"
    trip.mkdir()
    paths = resolve_writable_paths(
        trip, state_root=tmp_path / "state", sidecars_root=tmp_path / "side",
    )
    from unittest.mock import MagicMock
    process_mod.process_trip(trip, None, LIB, sink=MagicMock(), paths=paths)
    assert seen["paths"] is paths


def test_mac_read_folder_unchanged_for_sibling_sidecar(tmp_path: Path):
    trip = tmp_path / "trip"
    shutil.copytree(FIXTURES / "trip-anchor-simple", trip)
    media = sorted(p for p in trip.iterdir() if p.suffix.upper() == ".JPG")[0]
    _write_xmp(media.with_suffix(".xmp"), "2025:07:01 12:00:00+02:00")
    rows = {r.path: r for r in read_folder(trip)}
    assert rows[media].sidecar["XMP:DateTimeOriginal"] == "2025:07:01 12:00:00+02:00"


# --- fix round 1: date + offset selected together; QuickTime clock by maker --


def test_rejected_xmp_date_does_not_lend_its_offset(tmp_path: Path):
    # EXIF wins (no offset); the losing embedded XMP date's +05:00 must not
    # be applied to it.
    asset, exif = _build(tmp_path, {
        "EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
        "XMP:DateTimeOriginal": "2020:01:01 00:00:00+05:00",
    })
    assert exif.date_time_original == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert exif.time_zone is None


def test_quicktime_offset_does_not_leak_onto_exif_date(tmp_path: Path):
    _, exif = _build(tmp_path, {
        "EXIF:DateTimeOriginal": "2025:07:01 12:00:00",
        "QuickTime:CreateDate": "2025:07:01 10:00:00",
        "QuickTime:TimeZone": "+02:00",
    })
    assert exif.date_time_original == datetime(2025, 7, 1, 12, 0, tzinfo=UTC)
    assert exif.time_zone is None


def _build_media(tmp_path: Path, name: str, raw: dict):
    trip = tmp_path / "trip"
    trip.mkdir(exist_ok=True)
    media = trip / name
    media.write_bytes(b"x")
    return process_mod.build_rows(media, trip, ExifRow(path=media, raw=raw), LIB)


def test_insta360_create_date_is_local_wall_clock(tmp_path: Path):
    # Live evidence: VID_20240211_125116_00_052.insv (Bolivia, UTC-4) has
    # CreateDate 12:51:08 — the filename's local time, not UTC.
    asset, exif = _build_media(tmp_path, "VID_20240211_125116_00_052.insv", {
        "QuickTime:CreateDate": "2024:02:11 12:51:08",
        "QuickTime:Make": "Insta360",
    })
    assert asset.local_date_time == datetime(2024, 2, 11, 12, 51, 8, tzinfo=UTC)
    # No zone known → naive wall clock taken as UTC (unchanged behaviour).
    assert exif.date_time_original == datetime(2024, 2, 11, 12, 51, 8, tzinfo=UTC)


def test_insta360_go2_without_make_is_local_by_filename(tmp_path: Path):
    asset, exif = _build_media(tmp_path, "PRO_VID_20221109_140156_00_015.mp4", {
        "QuickTime:CreateDate": "2022:11:09 14:01:46",
        "QuickTime:TimeZone": "+01:00",
    })
    assert asset.local_date_time == datetime(2022, 11, 9, 14, 1, 46, tzinfo=UTC)
    assert exif.date_time_original == datetime(2022, 11, 9, 13, 1, 46, tzinfo=UTC)


def test_unknown_maker_quicktime_stays_utc(tmp_path: Path):
    asset, exif = _build_media(tmp_path, "GX010716.MP4", {
        "QuickTime:CreateDate": "2026:03:06 06:23:16",
        "QuickTime:TimeZone": 240,  # GoPro, exiftool -n: minutes
        "QuickTime:Model": "HERO13 Black",
    })
    # GPSDateTime (UTC) of the real clip is 06:23:18 → CreateDate is UTC.
    assert exif.date_time_original == datetime(2026, 3, 6, 6, 23, 16, tzinfo=UTC)
    assert asset.local_date_time == datetime(2026, 3, 6, 10, 23, 16, tzinfo=UTC)
    assert exif.time_zone == "UTC+4"


def test_apple_creation_date_with_offset_beats_create_date(tmp_path: Path):
    asset, exif = _build_media(tmp_path, "IMG_0001.MOV", {
        "QuickTime:Make": "Apple",
        "QuickTime:CreateDate": "2025:07:01 10:00:00",
        "QuickTime:CreationDate": "2025:07:01 12:00:05+02:00",
    })
    assert exif.date_time_original == datetime(2025, 7, 1, 10, 0, 5, tzinfo=UTC)
    assert asset.local_date_time == datetime(2025, 7, 1, 12, 0, 5, tzinfo=UTC)
    assert exif.time_zone == "UTC+2"
