from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immy.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _xmp_tags(xmp: Path) -> dict:
    out = subprocess.run(
        ["exiftool", "-j", "-n", "-G0", str(xmp)],
        capture_output=True, text=True, check=True,
    )
    import json
    blobs = json.loads(out.stdout)
    return blobs[0] if blobs else {}


def _write_gpx(path: Path, points: list[tuple[str, float, float]]) -> None:
    """points = [(iso_utc_time, lat, lon), ...]"""
    body = '<?xml version="1.0"?>\n<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">\n<trk><trkseg>\n'
    for t, lat, lon in points:
        body += f'  <trkpt lat="{lat}" lon="{lon}"><time>{t}</time></trkpt>\n'
    body += "</trkseg></trk>\n</gpx>\n"
    path.write_text(body)


def _stamp_jpg(dst: Path, *, dt: str, extra_args: list[str] | None = None) -> None:
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    dst.write_bytes(src.read_bytes())
    args = ["exiftool", "-overwrite_original", f"-EXIF:DateTimeOriginal={dt}"]
    if extra_args:
        args.extend(extra_args)
    args.append(str(dst))
    subprocess.run(args, check=True, capture_output=True)


def test_geotag_matches_nearby_track_point(tmp_path: Path):
    folder = tmp_path / "gpx-trip"
    folder.mkdir()
    # Image at 2026-04-01 10:00 local (Indian/Mauritius = UTC+4) → UTC 06:00.
    _stamp_jpg(folder / "IMG_0001.JPG", dt="2026:04:01 10:00:00")
    _write_gpx(folder / "track.gpx", [
        ("2026-04-01T05:58:00Z", -20.10, 57.10),
        ("2026-04-01T06:00:30Z", -20.20, 57.20),  # Δ30s from image — winner
        ("2026-04-01T06:30:00Z", -20.30, 57.30),
    ])
    (folder / "TRIP.md").write_text(
        "---\ntimezone: Indian/Mauritius\n---\n"
    )
    result = runner.invoke(app, ["audit", str(folder), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(folder / "IMG_0001.xmp")
    assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.20, abs=1e-4)
    assert float(tags["XMP:GPSLongitude"]) == pytest.approx(57.20, abs=1e-4)


def test_geotag_skips_outside_threshold(tmp_path: Path):
    folder = tmp_path / "gpx-far"
    folder.mkdir()
    _stamp_jpg(folder / "IMG_0001.JPG", dt="2026:04:01 10:00:00")
    # Track points 2 h away from image time → nothing flagged.
    _write_gpx(folder / "track.gpx", [
        ("2026-04-01T08:00:00Z", -20.1, 57.1),
        ("2026-04-01T08:05:00Z", -20.2, 57.2),
    ])
    (folder / "TRIP.md").write_text("---\ntimezone: Indian/Mauritius\n---\n")
    result = runner.invoke(app, ["audit", str(folder), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    xmp = folder / "IMG_0001.xmp"
    if xmp.exists():
        tags = _xmp_tags(xmp)
        assert "XMP:GPSLatitude" not in tags


def test_geotag_skips_when_no_timezone_and_no_offset(tmp_path: Path):
    # No tz in notes, no OffsetTimeOriginal, no suffix on date → can't
    # align to UTC → rule stands down.
    folder = tmp_path / "gpx-no-tz"
    folder.mkdir()
    _stamp_jpg(folder / "IMG_0001.JPG", dt="2026:04:01 10:00:00")
    _write_gpx(folder / "track.gpx", [
        ("2026-04-01T10:00:00Z", -20.2, 57.2),
    ])
    (folder / "TRIP.md").write_text("---\ntrip: no-tz\n---\n")
    result = runner.invoke(app, ["audit", str(folder), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    assert not (folder / "IMG_0001.xmp").exists() or \
        "XMP:GPSLatitude" not in _xmp_tags(folder / "IMG_0001.xmp")


def test_geotag_uses_offset_time_original(tmp_path: Path):
    # No timezone in notes, but EXIF:OffsetTimeOriginal set on the file.
    folder = tmp_path / "gpx-offset"
    folder.mkdir()
    _stamp_jpg(
        folder / "IMG_0001.JPG",
        dt="2026:04:01 10:00:00",
        extra_args=["-EXIF:OffsetTimeOriginal=+04:00"],
    )
    _write_gpx(folder / "track.gpx", [
        ("2026-04-01T06:00:00Z", -20.2, 57.2),
    ])
    (folder / "TRIP.md").write_text("---\ntrip: per-file-offset\n---\n")
    result = runner.invoke(app, ["audit", str(folder), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(folder / "IMG_0001.xmp")
    assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.2, abs=1e-4)


def test_geotag_skips_already_geotagged(tmp_path: Path):
    # File already has GPS → rule stands down (leaves it alone).
    folder = tmp_path / "gpx-have-gps"
    folder.mkdir()
    _stamp_jpg(
        folder / "IMG_0001.JPG",
        dt="2026:04:01 10:00:00",
        extra_args=[
            "-EXIF:GPSLatitudeRef=N", "-EXIF:GPSLatitude=10.0",
            "-EXIF:GPSLongitudeRef=E", "-EXIF:GPSLongitude=20.0",
        ],
    )
    _write_gpx(folder / "track.gpx", [
        ("2026-04-01T06:00:00Z", -20.2, 57.2),  # would overwrite if rule fired
    ])
    (folder / "TRIP.md").write_text("---\ntimezone: Indian/Mauritius\n---\n")
    result = runner.invoke(app, ["audit", str(folder), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    xmp = folder / "IMG_0001.xmp"
    if xmp.exists():
        tags = _xmp_tags(xmp)
        # If any XMP GPS was written, it wasn't from GPX. The rule shouldn't
        # have fired at all (EXIF GPS already present).
        lat = tags.get("XMP:GPSLatitude")
        if lat is not None:
            assert abs(float(lat) - 10.0) < 1e-3, f"rule overwrote existing GPS: {lat}"


def test_geotag_handles_gpx_with_default_namespace(tmp_path: Path):
    # GPX with a different common namespace (1.0 variants).
    folder = tmp_path / "gpx-ns"
    folder.mkdir()
    _stamp_jpg(folder / "IMG_0001.JPG", dt="2026:04:01 10:00:00")
    (folder / "track.gpx").write_text(
        '<?xml version="1.0"?>\n'
        '<gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0">\n'
        '<trk><trkseg>\n'
        '  <trkpt lat="-20.2" lon="57.2"><time>2026-04-01T06:00:00Z</time></trkpt>\n'
        '</trkseg></trk>\n</gpx>\n'
    )
    (folder / "TRIP.md").write_text("---\ntimezone: Indian/Mauritius\n---\n")
    result = runner.invoke(app, ["audit", str(folder), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(folder / "IMG_0001.xmp")
    assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.2, abs=1e-4)
