"""Tests for the DJI .SRT telemetry parser (`immy/srt.py`)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from immy import srt

FIXTURES = Path(__file__).parent / "fixtures" / "dji-srt-pair"


def test_parse_track_multiframe_rel_abs_alt_and_settings():
    frames = srt.parse_track(FIXTURES / "DJI_MULTI.SRT")
    assert len(frames) == 3

    # Combined `[rel_alt: .. abs_alt: ..]` bracket → both fields.
    f2 = frames[1]
    assert f2.latitude == 41.385100
    assert f2.longitude == 2.173400
    assert f2.rel_alt == 12.300
    assert f2.abs_alt == 75.060
    # Camera settings off the same cue.
    assert f2.iso == 100.0
    assert f2.shutter == "1/1000.0"
    assert f2.fnum == 2.8
    assert f2.ev == 0.0
    assert f2.focal_len == 24.00
    assert f2.datetime == datetime(2024, 8, 12, 18, 30, 1)
    assert f2.t_offset_s == 0.033

    # `ele` prefers abs_alt (MSL) for GPX.
    assert f2.ele == 75.060


def test_first_valid_fix_skips_null_island():
    frames = srt.parse_track(FIXTURES / "DJI_MULTI.SRT")
    # Frame 1 is (0, 0) pre-lock and must be skipped.
    assert frames[0].latitude == 0.0 and not frames[0].has_fix()
    fix = srt.first_valid_fix(frames)
    assert fix is not None
    assert fix.index == 2
    assert (fix.latitude, fix.longitude) == (41.385100, 2.173400)


def test_parse_summary_uses_first_valid_fix():
    tele = srt.parse(FIXTURES / "DJI_MULTI.SRT")
    # Coords come from the takeoff fix, not the (0,0) prelock frame.
    assert tele.latitude == 41.385100
    assert tele.longitude == 2.173400
    assert tele.altitude == 75.060  # abs_alt of the fix frame
    # Date is the first cue's wall-clock (frames are ~1 s apart).
    assert tele.datetime_original == datetime(2024, 8, 12, 18, 30, 0)


def test_legacy_bracketed_altitude_fixture():
    # The original single-frame fixture uses `[altitude: 120.0]` (no
    # rel/abs split) → lands in rel_alt, surfaces via .ele and parse().
    frames = srt.parse_track(FIXTURES / "DJI_0001.SRT")
    assert len(frames) == 1
    assert frames[0].rel_alt == 120.0
    assert frames[0].abs_alt is None
    assert frames[0].ele == 120.0
    tele = srt.parse(FIXTURES / "DJI_0001.SRT")
    assert (tele.latitude, tele.longitude) == (-20.296270, 57.407940)
    assert tele.altitude == 120.0


def test_parenthesised_gps_form(tmp_path: Path):
    txt = (
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "FrameCnt : 1\n2023-01-02 10:00:00,000\n"
        "GPS(-3.456000,12.789000,55.5) BAROMETER:55.5\n"
    )
    p = tmp_path / "old.SRT"
    p.write_text(txt)
    frames = srt.parse_track(p)
    assert len(frames) == 1
    assert frames[0].latitude == -3.456000
    assert frames[0].longitude == 12.789000
    assert frames[0].abs_alt == 55.5
    assert frames[0].has_fix()


def test_find_sibling(tmp_path: Path):
    media = tmp_path / "DJI_0001.MP4"
    media.write_bytes(b"")
    assert srt.find_sibling(media) is None
    (tmp_path / "DJI_0001.SRT").write_text("x")
    assert srt.find_sibling(media) == tmp_path / "DJI_0001.SRT"
