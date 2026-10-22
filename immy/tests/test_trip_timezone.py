"""trip-timezone on videos whose only date is QuickTime CreateDate (UTC on
most cameras, local on older Insta360s)."""

from __future__ import annotations

from pathlib import Path

from immy.exif import ExifRow
from immy.rules.trip_timezone import _propose


def _trip(tmp_path: Path, zone: str) -> Path:
    folder = tmp_path / "trip"
    folder.mkdir()
    (folder / "README.md").write_text(f"---\ntimezone: {zone}\n---\n# trip\n")
    return folder


def _patch(folder: Path, name: str, raw: dict) -> str | None:
    found = _propose([ExifRow(path=folder / name, raw=raw)], folder)
    return found[0].patch["DateTimeOriginal"] if found else None


def test_a_video_utc_clock_is_converted_not_relabelled(tmp_path: Path) -> None:
    # Transfăgărășan: DJI clock on UTC+2 (filename 16:17), CreateDate UTC 14:17,
    # Romania UTC+3 → 17:17+03:00, not 14:17+03:00.
    folder = _trip(tmp_path, "Europe/Bucharest")
    assert _patch(folder, "DJI_20250816161719_0002_D.MP4",
                  {"QuickTime:CreateDate": "2025:08:16 14:17:20"}) == "2025:08:16 17:17:20+03:00"


def test_an_apple_video_with_its_own_local_time_is_left_alone(tmp_path: Path) -> None:
    folder = _trip(tmp_path, "Europe/Rome")
    assert _patch(folder, "IMG_8014.MOV", {
        "QuickTime:CreateDate": "2026:06:25 14:23:12",
        "QuickTime:CreationDate": "2026:06:25 16:23:12+02:00",
    }) is None


def test_an_older_insta360_local_clock_is_labelled(tmp_path: Path) -> None:
    # CreateDate seconds from the filename's clock: local wall time.
    folder = _trip(tmp_path, "America/La_Paz")
    assert _patch(folder, "VID_20240211_125116_00_052.insv",
                  {"QuickTime:CreateDate": "2024:02:11 12:51:08"}) == "2024:02:11 12:51:08-04:00"


def test_an_insta360_x5_utc_clock_is_converted(tmp_path: Path) -> None:
    # Corsica: X5 still on Mauritius time (filename 18:07), CreateDate UTC 14:07,
    # Rome UTC+2 → 16:07+02:00.
    folder = _trip(tmp_path, "Europe/Rome")
    assert _patch(folder, "LRV_20260625_180744_01_051.lrv",
                  {"QuickTime:CreateDate": "2026:06:25 14:07:44"}) == "2026:06:25 16:07:44+02:00"


def test_a_photo_date_is_still_labelled_with_the_trip_offset(tmp_path: Path) -> None:
    folder = _trip(tmp_path, "Europe/Bucharest")
    assert _patch(folder, "DJI_20250816161714_0001_D.JPG",
                  {"EXIF:DateTimeOriginal": "2025:08:16 16:17:14"}) == "2025:08:16 16:17:14+03:00"


def test_a_known_x5_is_utc_even_when_its_clock_matches(tmp_path: Path) -> None:
    # Camera set to UTC+0 (or a renamed file): the model, not the filename, decides.
    folder = _trip(tmp_path, "Europe/Rome")
    assert _patch(folder, "VID_20260625_140744_00_051.insv", {
        "QuickTime:CreateDate": "2026:06:25 14:07:44", "QuickTime:Model": "Insta360 X5",
    }) == "2026:06:25 16:07:44+02:00"
    assert _patch(folder, "renamed.insv", {
        "QuickTime:CreateDate": "2026:06:25 14:07:44", "QuickTime:Make": "Insta360",
        "QuickTime:Model": "Insta360 X5",
    }) == "2026:06:25 16:07:44+02:00"


def test_a_utc_clock_keeps_its_instant_across_dst(tmp_path: Path) -> None:
    # 2025-10-26 00:30 UTC is 02:30 CEST; 01:30 UTC is 02:30 CET (the repeat).
    folder = _trip(tmp_path, "Europe/Rome")
    assert _patch(folder, "DJI_0001.MP4", {"QuickTime:CreateDate": "2025:10:26 00:30:00"}) \
        == "2025:10:26 02:30:00+02:00"
    assert _patch(folder, "DJI_0002.MP4", {"QuickTime:CreateDate": "2025:10:26 01:30:00"}) \
        == "2025:10:26 02:30:00+01:00"
    # Spring: 01:30 UTC is 03:30 CEST (02:00–03:00 doesn't exist).
    assert _patch(folder, "DJI_0003.MP4", {"QuickTime:CreateDate": "2025:03:30 01:30:00"}) \
        == "2025:03:30 03:30:00+02:00"


def test_go2_pro_and_lrv_names_read_their_own_clock(tmp_path: Path) -> None:
    folder = _trip(tmp_path, "Europe/Warsaw")
    # GO 2, local clock (seconds from the name) → labelled as is.
    assert _patch(folder, "PRO_VID_20221109_140156_00_015.mp4",
                  {"QuickTime:CreateDate": "2022:11:09 14:01:46"}) == "2022:11:09 14:01:46+01:00"
    # An LRV a zone from its name → UTC.
    assert _patch(folder, "LRV_20221109_150156_01_015.lrv",
                  {"QuickTime:CreateDate": "2022:11:09 14:01:56"}) == "2022:11:09 15:01:56+01:00"
