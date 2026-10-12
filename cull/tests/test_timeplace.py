from datetime import datetime

import pytest

from cull import timeplace as tp

MAVIC3 = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2022-01-30 12:29:38.078
[iso: 100] [shutter: 1/1250.0] [latitude: 0.0] [longitude: 0.0] [rel_alt: 26.900 abs_alt: 371.501] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">FrameCnt: 2, DiffTime: 33ms
2022-01-30 12:29:38.111
[iso: 100] [latitude: 50.23553] [longitude: 30.46274] </font>
"""

MINI = """1
00:00:00,000 --> 00:00:00,033
2023-05-01 09:00:01,500
[latitude : 38.10000] [longtitude : -0.80000]
"""

MAVIC_PRO = """1
00:00:00,000 --> 00:00:01,000
HOME(30.4627,50.2355) 2017.08.05 14:11:51
GPS(30.4630,50.2360,19) BAROMETER:1.9
"""

MATRICE = """1
00:00:00,000 --> 00:00:01,000
2019.01.02 10:00:00
GPS(37.6012,-0.7896,12.0M)
"""


def write(tmp_path, text):
    p = tmp_path / "a.SRT"
    p.write_text(text)
    return p


def test_srt_mavic3_skips_cue_line_and_zero_fix(tmp_path):
    wall, gps = tp.srt_first(write(tmp_path, MAVIC3))
    assert wall == datetime(2022, 1, 30, 12, 29, 38, 78000)
    assert gps == (50.23553, 30.46274)


def test_srt_mini_comma_and_longtitude(tmp_path):
    wall, gps = tp.srt_first(write(tmp_path, MINI))
    assert wall == datetime(2023, 5, 1, 9, 0, 1, 500000) and gps == (38.1, -0.8)


def test_srt_mavic_pro_dots_lon_first(tmp_path):
    wall, gps = tp.srt_first(write(tmp_path, MAVIC_PRO))
    assert wall == datetime(2017, 8, 5, 14, 11, 51) and gps == (50.236, 30.463)


def test_srt_matrice_lat_first(tmp_path):
    assert tp.srt_first(write(tmp_path, MATRICE))[1] == (37.6012, -0.7896)


def test_srt_wins_and_is_wall():
    ct = tp.capture_time({"DateTimeOriginal": "2022:01:30 10:00:00"}, datetime(2022, 1, 30, 12, 29, 38), 0, None)
    assert ct == tp.CaptureTime("2022-01-30T12:29:38", "srt", "wall")


def test_exif_without_offset_is_wall():
    ct = tp.capture_time({"DateTimeOriginal": "2022:01:30 12:31:00"}, None, 0, None)
    assert (ct.time, ct.src, ct.kind) == ("2022-01-30T12:31:00", "exif", "wall")


def test_exif_with_offset_is_instant_shown_in_gps_zone():
    row = {"DateTimeOriginal": "2022:12:14 15:20:01", "OffsetTimeOriginal": "+00:00"}
    ct = tp.capture_time(row, None, 0, (38.72, -9.14))  # Lisbon, WET in December
    assert (ct.time, ct.kind, ct.utc, ct.zone) == ("2022-12-14T15:20:01", "instant", "2022-12-14T15:20:01Z",
                                                   "Europe/Lisbon")


def test_quicktime_createdate_is_utc_unless_local_clock_make():
    row = {"CreateDate": "2022:01:30 10:29:38", "Make": "DJI"}
    ct = tp.capture_time(row, None, 0, (50.23553, 30.46274))  # Kyiv, UTC+2 in January
    assert (ct.time, ct.src, ct.kind, ct.utc) == ("2022-01-30T12:29:38", "qt-create", "instant", "2022-01-30T10:29:38Z")
    ins = tp.capture_time({"CreateDate": "2022:01:30 10:29:38", "Make": "Insta360"}, None, 0, None)
    assert (ins.time, ins.kind) == ("2022-01-30T10:29:38", "wall")
    nozone = tp.capture_time({"CreateDate": "2022:01:30 10:29:38"}, None, 0, None)
    assert (nozone.time, nozone.zone) == ("2022-01-30T10:29:38", "UTC")


def test_zero_dates_fall_through_to_mtime():
    ct = tp.capture_time({"CreateDate": "0000:00:00 00:00:00"}, None, 1_700_000_000, None)
    assert ct.src == "mtime" and ct.kind == "guess"


def test_exif_gps_rejects_zero_and_out_of_range():
    assert tp.exif_gps({"GPSLatitude": 38.1, "GPSLongitude": -0.8}) == (38.1, -0.8)
    assert tp.exif_gps({"GPSLatitude": 0, "GPSLongitude": 0}) is None
    assert tp.exif_gps({"GPSLatitude": 91, "GPSLongitude": 0.5}) is None
    assert tp.exif_gps({}) is None


def test_exif_batch_reads_real_files(tmp_path, media_tools):
    p = media_tools["jpeg"](tmp_path / "a.JPG")
    rows = tp.exif_batch([p])
    assert rows[str(p)]["DateTimeOriginal"] == "2022:01:30 12:31:00" and rows[str(p)]["Make"] == "DJI"


def test_exif_batch_without_exiftool_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(tp.shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError, match="exiftool"):
        tp.exif_batch([tmp_path / "a.JPG"])


def test_exif_batch_fails_when_exiftool_errors_without_json(tmp_path, monkeypatch):
    import subprocess

    fake = subprocess.CompletedProcess(args=["exiftool"], returncode=2, stdout="", stderr="boom")
    monkeypatch.setattr(tp.subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match="boom"):
        tp.exif_batch([tmp_path / "a.JPG"])


def test_exif_batch_keeps_json_rows_when_exiftool_exits_nonzero(tmp_path, monkeypatch):
    import subprocess

    out = '[{"SourceFile": "/x/a.JPG", "Make": "DJI"}, {"SourceFile": "/x/b.JPG", "Error": "bad"}]'
    fake = subprocess.CompletedProcess(args=["exiftool"], returncode=1, stdout=out, stderr="")
    monkeypatch.setattr(tp.subprocess, "run", lambda *a, **k: fake)
    assert tp.exif_batch([tmp_path / "a.JPG", tmp_path / "b.JPG"]) == {"/x/a.JPG": {"SourceFile": "/x/a.JPG", "Make": "DJI"}}


def test_exif_batch_non_ascii_filename_round_trips(tmp_path, media_tools):
    p = media_tools["jpeg"](tmp_path / "Мавік café.JPG")
    rows = tp.exif_batch([p])
    assert rows[str(p)]["Make"] == "DJI"
