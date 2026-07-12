from __future__ import annotations

from datetime import datetime
from pathlib import Path

from immy import dates
from immy.exif import ExifRow


def test_resolve_prefers_exif_over_filename(tmp_path: Path) -> None:
    p = tmp_path / "IMG_20200101_120000.jpg"
    p.write_bytes(b"")
    row = ExifRow(path=p, raw={"EXIF:DateTimeOriginal": "2021:06:15 10:30:00"})
    auth = dates.resolve(row)
    assert auth.source == "exif"
    assert auth.dt == datetime(2021, 6, 15, 10, 30, 0)


def test_resolve_falls_back_to_mtime_when_no_other_signal(tmp_path: Path) -> None:
    p = tmp_path / "randomname.jpg"
    p.write_bytes(b"")
    row = ExifRow(path=p, raw={})
    auth = dates.resolve(row)
    assert auth.source == "mtime"


def test_resolve_rejects_implausible_future_exif_date(tmp_path: Path) -> None:
    # Diagnosed 2026-07-12: a batch of videos carried a firmware placeholder
    # EXIF date of 2036-01-01T23:59:59 (clock-battery reset), which parses
    # fine but isn't a real capture time -- must not be trusted as 'exif'.
    p = tmp_path / "DJI_9999.MOV"
    p.write_bytes(b"")
    row = ExifRow(path=p, raw={"EXIF:DateTimeOriginal": "2036:01:01 23:59:59"})
    auth = dates.resolve(row)
    assert auth.source == "mtime"  # implausible EXIF rejected, falls through


def test_resolve_rejects_implausible_ancient_exif_date(tmp_path: Path) -> None:
    p = tmp_path / "IMG_0001.jpg"
    p.write_bytes(b"")
    row = ExifRow(path=p, raw={"EXIF:DateTimeOriginal": "1970:01:01 00:00:00"})
    auth = dates.resolve(row)
    assert auth.source == "mtime"


def test_resolve_falls_through_to_next_exif_key_when_first_is_implausible(tmp_path: Path) -> None:
    p = tmp_path / "IMG_0002.jpg"
    p.write_bytes(b"")
    row = ExifRow(path=p, raw={
        "XMP:DateTimeOriginal": "2036:01:01 23:59:59",
        "EXIF:DateTimeOriginal": "2022:03:04 08:00:00",
    })
    auth = dates.resolve(row)
    assert auth.source == "exif"
    assert auth.dt == datetime(2022, 3, 4, 8, 0, 0)
