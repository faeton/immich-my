"""Placing GPS-less shots (Meta glasses) from the geotagged shots around them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from immy import locate

UTC = timezone.utc
PLUS4 = timezone(timedelta(hours=4))


def _phone(wall: str, lat: float, lon: float, off=PLUS4) -> locate.Point:
    w = datetime.fromisoformat(wall)
    return locate.Point(w, w.replace(tzinfo=off), lat, lon)


def _photo(wall: str, zone=None) -> locate.Target:
    w = datetime.fromisoformat(wall)
    return locate.Target("p", "/lib/2026/03/photo-1.heic", False, w, w.replace(tzinfo=UTC), zone, None)


def _video(instant_utc: str, zone=None, wall: str | None = None) -> locate.Target:
    i = datetime.fromisoformat(instant_utc).replace(tzinfo=UTC)
    return locate.Target("v", "/lib/2026/03/video-1.mov", True,
                         datetime.fromisoformat(wall) if wall else i.replace(tzinfo=None), i, zone, None)


TRACK = [_phone("2026-03-06 10:00:00", -20.30, 57.37), _phone("2026-03-06 10:20:00", -20.31, 57.37),
         _phone("2026-03-06 13:00:00", -20.45, 57.70)]


def test_a_photo_is_placed_on_the_phones_wall_clock_and_gets_its_offset() -> None:
    got = locate.place([_photo("2026-03-06 10:02:00")], TRACK)
    assert len(got) == 1 and got[0].confidence == "high"
    assert (round(got[0].lat, 2), round(got[0].lon, 2)) == (-20.30, 57.37)
    assert got[0].dto == "2026:03:06 10:02:00+04:00"


def test_a_video_is_placed_on_its_instant_and_dated_locally() -> None:
    # 06:05 UTC is 10:05 at +4.
    got = locate.place([_video("2026-03-06 06:05:00")], TRACK)
    assert got[0].confidence == "high" and got[0].dto == "2026:03:06 10:05:00+04:00"


def test_a_zoned_shot_keeps_its_date() -> None:
    got = locate.place([_video("2026-03-06 06:05:00", zone="Indian/Mauritius", wall="2026-03-06 10:05:00")], TRACK)
    assert got[0].dto is None and got[0].lat is not None


def test_nothing_within_reach_is_left_out() -> None:
    assert locate.place([_photo("2026-03-07 22:00:00")], TRACK) == []


def test_a_zone_change_in_the_bracket_leaves_the_date_alone() -> None:
    track = [_phone("2026-03-06 10:00:00", -20.30, 57.37), _phone("2026-03-06 10:04:00", -20.30, 57.37,
                                                                   off=timezone(timedelta(hours=3)))]
    got = locate.place([_photo("2026-03-06 10:02:00")], track)
    assert got and got[0].dto is None


def test_patch_writes_signed_degrees() -> None:
    p = locate.Placement(_photo("2026-03-06 10:02:00"), -20.3, 57.37, "high", "x", "2026:03:06 10:02:00+04:00")
    assert locate.patch(p) == {"GPSLatitude": "-20.300000", "GPSLongitude": "57.370000",
                               "DateTimeOriginal": "2026:03:06 10:02:00+04:00"}


def test_interpolating_across_a_flight_is_left_out() -> None:
    # 10:00 Mauritius, 14:00 Paris-ish (8,000 km): a photo at 12:00 between them is unknowable.
    track = [_phone("2026-03-06 10:00:00", -20.30, 57.37), _phone("2026-03-06 14:00:00", 48.85, 2.35)]
    assert locate.place([_photo("2026-03-06 12:00:00")], track) == []


def test_a_short_interpolation_is_kept() -> None:
    track = [_phone("2026-03-06 10:00:00", -20.30, 57.37), _phone("2026-03-06 10:40:00", -20.40, 57.40)]
    got = locate.place([_photo("2026-03-06 10:20:00")], track)
    assert got and got[0].confidence == "medium" and "interpolated" in got[0].basis
