"""`immy takeout redate`: linking, dating, zoning, twins. Synthetic only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from immy import takeout_redate as tr

PREFIX = "/staging/google-takeout"
UTC = timezone.utc


def _json(folder: Path, name: str, title: str, when: datetime, geo=None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = {"title": title, "photoTakenTime": {"timestamp": str(int(when.timestamp()))}}
    if geo:
        body["geoData"] = {"latitude": geo[0], "longitude": geo[1]}
    (folder / name).write_text(json.dumps(body))


def _target(rel: str, local: datetime, reason="placeholder", gps=(None, None), aid="a1"):
    return tr.Target(aid, rel, local, gps[0], gps[1], reason)


def _plan(tmp_path, targets, index, zone=None, library_root=None):
    return tr.plan(targets, index, takeout_root=tmp_path / "takeout",
                   staging_prefix=PREFIX, neighbour_zone=lambda *_: zone,
                   library_root=library_root)


FOLDER_2019 = "unpacked/Takeout/Google Photos/Photos from 2019"
STAGED = f"{PREFIX}/{FOLDER_2019}/IMG_1711(1).MP4"
SHOT = datetime(2019, 11, 15, 1, 43, 25, tzinfo=UTC)   # evening of the 14th in Chicago
PLACEHOLDER = datetime(2019, 1, 1, 12, 0, 0)


def test_manifest_index_covers_promote_dates_and_collision_names() -> None:
    rows = [(42, STAGED, "2026-06-20T18:10:05", None),
            (43, f"{PREFIX}/x/IMG_9.JPG", "2026-06-21T00:00:00", "2019-03-01T10:00:00")]
    idx = tr.manifest_index(rows)
    assert idx["2026/06/IMG_1711(1).MP4"] == {STAGED}
    assert idx["2026/06/IMG_1711(1)__42.MP4"] == {STAGED}
    # Promoted under its pre-fix date, so both folders are candidates.
    assert f"{PREFIX}/x/IMG_9.JPG" in idx["2019/03/IMG_9.JPG"]
    assert f"{PREFIX}/x/IMG_9.JPG" in idx["2026/06/IMG_9.JPG"]


def test_placeholder_gets_jsons_instant_on_the_local_clock(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER, gps=(41.88, -87.63))],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert fix.problem is None
    assert fix.zone_source == "file gps"
    assert fix.xmp == "2019:11:14 19:43:25-06:00"


def test_zone_falls_back_to_json_gps_then_nearby_shots_then_utc(tmp_path) -> None:
    folder = tmp_path / "takeout" / FOLDER_2019
    _json(folder, "IMG_1.JPG.supplemental-metadata(1).json", "IMG_1.JPG", SHOT, geo=(41.88, -87.63))
    _json(folder, "IMG_2.JPG.supplemental-metadata(1).json", "IMG_2.JPG", SHOT)
    idx = {f"2026/06/IMG_{i}(1).JPG": {f"{PREFIX}/{FOLDER_2019}/IMG_{i}(1).JPG"} for i in (1, 2)}
    t1 = _target("2026/06/IMG_1(1).JPG", PLACEHOLDER, aid="1")
    t2 = _target("2026/06/IMG_2(1).JPG", PLACEHOLDER, aid="2")
    f1, f2 = _plan(tmp_path, [t1, t2], idx, zone="UTC+2")
    assert (f1.zone_source, f1.xmp) == ("json gps", "2019:11:14 19:43:25-06:00")
    assert (f2.zone_source, f2.xmp) == ("nearby shots", "2019:11:15 03:43:25+02:00")
    _, f2 = _plan(tmp_path, [t1, t2], idx, zone=None)
    assert (f2.zone_source, f2.xmp) == ("utc", "2019:11:15 01:43:25+00:00")


def test_placeholder_source_must_sit_in_its_year_folder(tmp_path) -> None:
    other = f"{PREFIX}/unpacked/Takeout/Google Photos/Photos from 2023/IMG_1711(1).MP4"
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    _json(tmp_path / "takeout/unpacked/Takeout/Google Photos/Photos from 2023",
          "IMG_1711.HEIC.supplemental-metadata(1).json", "IMG_1711.HEIC",
          datetime(2023, 5, 1, tzinfo=UTC))
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER)],
                 {"2026/06/IMG_1711(1).MP4": {STAGED, other}})
    assert fix.problem is None
    assert fix.staging == STAGED


def test_utc_asset_is_rezoned_only_if_it_shows_googles_instant(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT, geo=(41.88, -87.63))
    idx = {"2026/06/IMG_1711(1).MP4": {STAGED}}
    shows_utc = SHOT.replace(tzinfo=None)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", shows_utc, reason="utc")], idx)
    assert fix.problem is None and fix.xmp == "2019:11:14 19:43:25-06:00"
    # Its own (camera) date disagrees with Google's: hands off.
    own = shows_utc + timedelta(hours=7, minutes=3)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", own, reason="utc")], idx)
    assert fix.problem == "own date disagrees with takeout"


def test_utc_without_any_zone_is_left_alone(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", SHOT.replace(tzinfo=None), reason="utc")],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert fix.problem == "still no zone"


def test_agreeing_duplicate_sources_are_one_answer(tmp_path) -> None:
    other_folder = "unpacked/Takeout/Google Photos/Archive"
    for f in (FOLDER_2019, other_folder):
        _json(tmp_path / "takeout" / f, "IMG_1711.HEIC.supplemental-metadata(1).json",
              "IMG_1711.HEIC", SHOT)
    idx = {"2026/06/IMG_1711(1).MP4": {STAGED, f"{PREFIX}/{other_folder}/IMG_1711(1).MP4"}}
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER)], idx)
    assert fix.problem is None


def test_no_source_and_no_json(tmp_path) -> None:
    a, b = _plan(tmp_path, [_target("2026/06/X.JPG", PLACEHOLDER, aid="x"),
                            _target("2026/06/IMG_1711(1).MP4", PLACEHOLDER, aid="y")],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert a.problem == "no takeout source"
    assert b.problem == "no json, no datable neighbours"


def test_neighbours_need_both_sides_and_agreement(tmp_path) -> None:
    folder = tmp_path / "takeout" / FOLDER_2019
    t0 = datetime(2018, 5, 7, 10, 0, tzinfo=UTC)
    _json(folder, "IMG_0210.mp4.supplemental-metadata.json", "IMG_0210.mp4", t0)
    _json(folder, "IMG_0214.mp4.supplemental-metadata.json", "IMG_0214.mp4", t0 + timedelta(hours=4))
    got = tr.neighbour_taken(folder / "IMG_0212(1).mp4")
    assert got and got.instant == t0 + timedelta(hours=2) and got.source == "neighbour-file"
    # Another device's .HEIC with that number doesn't count.
    assert tr.neighbour_taken(folder / "IMG_0212(1).HEIC") is None
    # Neighbours a week apart: not one session, no answer.
    _json(folder, "IMG_0310.mp4.supplemental-metadata.json", "IMG_0310.mp4", t0)
    _json(folder, "IMG_0312.mp4.supplemental-metadata.json", "IMG_0312.mp4", t0 + timedelta(days=7))
    assert tr.neighbour_taken(folder / "IMG_0311(1).mp4") is None


@pytest.mark.parametrize("name,expect", [
    ("Europe/Lisbon", "Europe/Lisbon"), ("UTC+2", "UTC+02:00"), ("UTC-03:30", "UTC-03:30"),
    ("UTC", None), ("UTC+0", None), ("nonsense", None), (None, None),
])
def test_parse_zone(name, expect) -> None:
    z = tr.parse_zone(name)
    assert (tr.zone_label(z) if z else None) == expect


def test_original_name() -> None:
    assert tr.original_name("IMG_1711(1).MP4") == "IMG_1711.MP4"
    assert tr.original_name("IMG_0076(1)__66958.MP4") == "IMG_0076.MP4"
    assert tr.original_name("IMG_0480__172431.HEIC") == "IMG_0480.HEIC"
    assert tr.original_name("IMG_1.JPG") == "IMG_1.JPG"


def test_twins() -> None:
    def c(aid, delta, clip=None):
        return tr.Candidate(aid, SHOT + delta, None, clip)
    assert tr.is_twin(SHOT, c("x", timedelta(seconds=1)))
    assert tr.is_twin(SHOT, c("x", timedelta(hours=-6)))          # original zoned wrongly
    assert not tr.is_twin(SHOT, c("x", timedelta(hours=6, seconds=30)))
    assert not tr.is_twin(SHOT, c("x", timedelta(days=2)))
    assert not tr.is_twin(SHOT, c("x", timedelta(0), clip=0.4))   # same name, other photo
    assert tr.pick_twin(SHOT, [c("a", timedelta(0)), c("b", timedelta(days=400))]).asset_id == "a"
    assert tr.pick_twin(SHOT, [c("a", timedelta(0)), c("b", timedelta(seconds=1))]) is None
