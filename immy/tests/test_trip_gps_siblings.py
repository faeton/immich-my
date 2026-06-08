"""Unit tests for the trip-gps-from-siblings rule: the _locate bracketing
state machine and the _propose source/target wiring (null-island repair,
MIN_SOURCE_POINTS guard, per-camera-day grouping)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from immy.exif import ExifRow
from immy.rules.trip_gps_siblings import (
    MIN_SOURCE_POINTS,
    _locate,
    _propose,
)


def _t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 4, 1, h, m, s)


# --- _locate: bracketing state machine ---------------------------------

def test_high_snaps_to_nearest_endpoint_not_interpolated():
    # 8-min bracket, endpoints 1° of lon apart (spatially wide). Target is
    # 3 min from `before`. HIGH must return before's actual coords, NOT the
    # interpolated ~0.375 lon — that's the bug both reviewers flagged.
    points = [(_t(10, 0), 0.0, 0.0), (_t(10, 8), 0.0, 1.0)]
    lat, lon, conf, basis = _locate(points, _t(10, 3))
    assert conf == "high"
    assert (lat, lon) == (0.0, 0.0)
    assert "nearest fix" in basis


def test_exact_timestamp_match_is_high_on_that_point():
    points = [(_t(10, 0), -20.0, 15.0), (_t(11, 0), -21.0, 16.0)]
    lat, lon, conf, _ = _locate(points, _t(11, 0))
    assert conf == "high"
    assert (lat, lon) == (-21.0, 16.0)


def test_stationary_bracket_is_medium_regardless_of_gap():
    # Endpoints ~0.7 km apart over an hour → camera parked → MEDIUM even
    # though the 30-min gap blows past the HIGH window.
    points = [(_t(10, 0), -20.0, 15.0), (_t(11, 0), -20.005, 15.005)]
    lat, lon, conf, basis = _locate(points, _t(10, 30))
    assert conf == "medium"
    assert "stationary" in basis
    assert -20.005 <= lat <= -20.0 and 15.0 <= lon <= 15.005  # interpolated within box


def test_moving_bracket_interpolates_medium():
    # ~70 km apart, target 20 min from `before` (≤90 min) → MEDIUM, interpolated.
    points = [(_t(10, 0), -20.0, 15.0), (_t(10, 40), -20.5, 15.5)]
    lat, lon, conf, basis = _locate(points, _t(10, 20))
    assert conf == "medium"
    assert "interpolated" in basis
    assert lat == -20.25 and lon == 15.25  # exact midpoint at 20/40


def test_moving_bracket_beyond_medium_window_skips():
    # >2 km, both endpoints >90 min from target, span ≤6 h → no trust → None.
    points = [(_t(10, 0), -20.0, 15.0), (_t(14, 0), -22.0, 16.0)]
    assert _locate(points, _t(12, 0)) is None


def test_void_bracket_falls_back_to_nearest_only():
    # 10-h span > MAX_BRACKET → bracket distrusted → nearest-only time gate.
    points = [(_t(2, 0), -20.0, 15.0), (_t(12, 0), -21.0, 16.0)]
    near_hi = _locate(points, _t(2, 3))
    assert near_hi[2] == "high" and (near_hi[0], near_hi[1]) == (-20.0, 15.0)
    assert "no bracket" in near_hi[3]
    near_med = _locate(points, _t(2, 30))
    assert near_med[2] == "medium"
    assert _locate(points, _t(5, 0)) is None  # 3 h from nearest → skip


def test_before_first_and_after_last_use_nearest():
    points = [(_t(10, 0), -20.0, 15.0), (_t(11, 0), -21.0, 16.0)]
    first = _locate(points, _t(9, 58))
    assert first[2] == "high" and (first[0], first[1]) == (-20.0, 15.0)
    last = _locate(points, _t(11, 2))
    assert last[2] == "high" and (last[0], last[1]) == (-21.0, 16.0)
    assert _locate(points, _t(8, 0)) is None  # 2 h before first → skip


# --- _propose: source/target wiring ------------------------------------

def _row(name: str, *, dt: str | None = None, lat=None, lon=None,
         model: str = "CamX") -> ExifRow:
    raw: dict = {"EXIF:Model": model}
    if dt is not None:
        raw["EXIF:DateTimeOriginal"] = dt
    if lat is not None:
        raw["Composite:GPSLatitude"] = lat
        raw["Composite:GPSLongitude"] = lon
    return ExifRow(path=Path(f"/tmp/{name}"), raw=raw)


def _sources(n: int) -> list[ExifRow]:
    # n geotagged frames one minute apart at one spot.
    return [
        _row(f"src{i}.jpg", dt=f"2026:04:01 10:{i:02d}:00", lat=-20.0, lon=15.0)
        for i in range(n)
    ]


def test_null_island_target_gets_repaired():
    rows = _sources(MIN_SOURCE_POINTS)
    target = _row("nullisland.mp4", dt="2026:04:01 10:02:30", lat=0.0, lon=0.0)
    findings = _propose(rows + [target], Path("/tmp"))
    hit = [f for f in findings if f.path.name == "nullisland.mp4"]
    assert len(hit) == 1
    assert hit[0].patch["GPSLatitude"] == "-20.000000"
    # The valid sources are never targets.
    assert not any(f.path.name.startswith("src") for f in findings)


def test_below_min_source_points_yields_nothing():
    rows = _sources(MIN_SOURCE_POINTS - 1)
    target = _row("gap.mp4", dt="2026:04:01 10:01:30")
    assert _propose(rows + [target], Path("/tmp")) == []


def test_dateless_target_is_skipped():
    rows = _sources(MIN_SOURCE_POINTS)
    target = _row("nodate.mp4")  # no date, missing path → resolves to None
    findings = _propose(rows + [target], Path("/tmp"))
    assert not any(f.path.name == "nodate.mp4" for f in findings)


def test_medium_findings_grouped_per_camera_day_high_ungrouped():
    rows = _sources(MIN_SOURCE_POINTS)
    high = _row("near.mp4", dt="2026:04:01 10:02:30", model="DJI")     # ≤5 min → HIGH
    med = _row("far.mp4", dt="2026:04:01 11:20:00", model="Insta360")  # ~78 min → MEDIUM
    findings = {f.path.name: f for f in _propose(rows + [high, med], Path("/tmp"))}
    assert findings["near.mp4"].confidence == "high"
    assert findings["near.mp4"].group == ""
    assert findings["far.mp4"].confidence == "medium"
    assert findings["far.mp4"].group == "siblings-gps:Insta360:2026-04-01"
