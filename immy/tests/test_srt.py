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
        "GPS(-3.456000,12.789000,55.5M) BAROMETER:55.5\n"
    )
    p = tmp_path / "old.SRT"
    p.write_text(txt)
    frames = srt.parse_track(p)
    assert len(frames) == 1
    assert frames[0].latitude == -3.456000
    assert frames[0].longitude == 12.789000
    assert frames[0].abs_alt is None  # unit-suffixed field not a verified MSL altitude
    assert frames[0].has_fix()


def test_find_sibling(tmp_path: Path):
    media = tmp_path / "DJI_0001.MP4"
    media.write_bytes(b"")
    assert srt.find_sibling(media) is None
    (tmp_path / "DJI_0001.SRT").write_text("x")
    assert srt.find_sibling(media) == tmp_path / "DJI_0001.SRT"


def _one(tmp_path: Path, body: str):
    p = tmp_path / "x.SRT"
    p.write_text("1\n00:00:00,000 --> 00:00:01,000\n" + body)
    return srt.parse_track(p)[0]


def test_longtitude_misspelling(tmp_path: Path):
    f = _one(tmp_path, (
        "2021-06-20 10:11:12.123\n[iso: 100] [latitude: 41.123456] "
        "[longtitude: 2.123456] [rel_alt: 1.3 abs_alt: -12.0]\n"))
    assert (f.latitude, f.longitude, f.abs_alt) == (41.123456, 2.123456, -12.0)
    assert f.has_fix()


# Provenance: excerpts of JuanIrache/DJI_SRT_Parser samples (MIT, (c) 2018 Juan Irache).
MATRICE_300 = (  # samples/matrice_300.srt -- lat-first, `M` unit
    "2022.06.21 16:06:17\nGPS(36.6146,-6.1120,0.0M) BAROMETER:0.3M\n"
)
OLD_FORMAT = (  # samples/old_format.SRT -- lon-first beside HOME()
    "HOME(149.0251,-20.2532) 2017.8.5 14:11:51\n"
    "GPS(149.0251,-20.2533,16) Hb:1.9 Hs:1.9\nISO:100 TV:60 EV: 0 IR:F2.8\n"
)


def test_matrice_300_is_lat_first_both_readings_in_range(tmp_path: Path):
    # (36.6, -6.1) also parses as lon=36.6/lat=-6.1: the M-unit dialect decides.
    f = _one(tmp_path, MATRICE_300)
    assert (f.latitude, f.longitude) == (36.6146, -6.1120)
    assert f.datetime == datetime(2022, 6, 21, 16, 6, 17)
    assert f.abs_alt is None and f.rel_alt is None


def test_old_format_is_lon_first_with_dotted_unpadded_date(tmp_path: Path):
    f = _one(tmp_path, OLD_FORMAT)
    assert (f.latitude, f.longitude) == (-20.2533, 149.0251)
    assert f.datetime == datetime(2017, 8, 5, 14, 11, 51)
    assert f.abs_alt is None and f.rel_alt is None


def test_home_alone_is_not_a_dialect_signature(tmp_path: Path):
    # HOME read both ways agrees with either order; both in range -> no fix.
    f = _one(tmp_path, "HOME(8.54,47.37) 2017.8.5 14:11:51\nGPS(8.54,47.37,12)\n")
    assert not f.has_fix()


def test_lat_first_unitless_file_with_home_resolves_by_range(tmp_path: Path):
    # lat-first coords (36.6, 120.1): the lon-first reading has lat 120 -> impossible.
    f = _one(tmp_path, "HOME(36.6,120.1) 2017.8.5 14:11:51\nGPS(36.6001,120.1001,12)\n")
    assert (f.latitude, f.longitude) == (36.6001, 120.1001)


def test_order_is_decided_per_file_with_labelled_evidence_in_other_cue(tmp_path: Path):
    p = tmp_path / "x.SRT"
    p.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n[latitude: 47.37] [longitude: 8.54]\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nGPS(8.5401,47.3701,12)\n"
    )
    fr = srt.parse_track(p)
    assert [(f.latitude, f.longitude, f.has_fix()) for f in fr] == [
        (47.37, 8.54, True), (47.3701, 8.5401, True)]


def test_labelled_evidence_picks_lat_first_too(tmp_path: Path):
    p = tmp_path / "x.SRT"
    p.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n[latitude: 8.54] [longitude: 47.37]\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nGPS(8.5401,47.3701,12)\n"
    )
    assert [(f.latitude, f.longitude) for f in srt.parse_track(p)][1] == (8.5401, 47.3701)


def test_per_file_order_is_constrained_by_every_cue(tmp_path: Path):
    p = tmp_path / "x.SRT"
    p.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nGPS(8.5,47.3,1)\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nGPS(149.0,-20.2,1)\n"
    )
    # Cue 2 is only valid lon-first, so the whole file is lon-first.
    assert [(f.latitude, f.longitude) for f in srt.parse_track(p)] == [(47.3, 8.5), (-20.2, 149.0)]


def test_unlabelled_both_in_range_emits_nothing(tmp_path: Path):
    assert not _one(tmp_path, "GPS(12.789,-3.456,55)\n").has_fix()


def test_unlabelled_single_valid_order_is_used(tmp_path: Path):
    f = _one(tmp_path, "GPS(-20.5,149.0,3)\n")
    assert (f.latitude, f.longitude) == (-20.5, 149.0)


def test_impossible_both_ways_emits_nothing(tmp_path: Path):
    assert not _one(tmp_path, "GPS(200.5,149.0,3)\n").has_fix()


def test_below_sea_level_altitude_roundtrips_negative(tmp_path: Path):
    import subprocess

    from immy import sidecar
    from immy.exif import ExifRow
    from immy.rules.dji_srt import _propose_gps

    media = tmp_path / "DJI_0001.MP4"
    media.write_bytes(b"")
    (tmp_path / "DJI_0001.SRT").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n2021-06-20 10:11:12.123\n"
        "[latitude: 41.1] [longitude: 2.1] [rel_alt: 1.3 abs_alt: -12.0]\n"
    )
    (finding,) = _propose_gps([ExifRow(path=media, raw={})], tmp_path)
    assert finding.patch["GPSAltitude"] == "12.00"
    xmp = sidecar.write(media, finding.patch)
    out = subprocess.run(
        ["exiftool", "-n", "-s3", "-Composite:GPSAltitude", str(xmp)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert float(out) == -12.0


def test_m_signature_and_home_evidence_collected_from_labelled_cues(tmp_path: Path):
    p = tmp_path / "x.SRT"
    p.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n[latitude: 47.37] [longitude: 8.54] GPS(47.37,8.54,0M)\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nHOME(47.37,8.54)\nGPS(47.3701,8.5401,12)\n"
    )
    fr = srt.parse_track(p)
    assert [(f.latitude, f.longitude) for f in fr] == [(47.37, 8.54), (47.3701, 8.5401)]
