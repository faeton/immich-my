"""Tests for `immy match` pure logic (placement + report).

Covers: clean geo match, extends-a-trip, brand-new trip, date-only
(GPS-less) placement, folder-spans-two-trips, all-duplicates, album
bound recomputation, and the snapshot v1 guard.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from immy import match, snapshot


# --- fixtures -------------------------------------------------------------

BCN = match.ExistingTrip(
    name="2024-05-barcelona", source="album",
    start=datetime(2024, 5, 1, 12, 0), end=datetime(2024, 5, 3, 12, 0),
    lat=41.40, lon=2.10, radius_km=2.0, asset_count=20,
)
TYO = match.ExistingTrip(
    name="2024-06-tokyo", source="cluster",
    start=datetime(2024, 6, 1, 9, 0), end=datetime(2024, 6, 4, 9, 0),
    lat=35.68, lon=139.69, radius_km=8.0, asset_count=40,
)
TRIPS = [BCN, TYO]


def _item(sub, when, lat=None, lon=None, dup=None, size=1000):
    return match.InboundItem(
        path=Path(f"/in/{sub}/x.jpg"), subfolder=sub, size=size,
        when=when, lat=lat, lon=lon, asset_type="IMAGE", dup_kind=dup,
    )


# --- place() --------------------------------------------------------------


def test_place_geo_matched():
    p = match.place(datetime(2024, 5, 2, 10), 41.41, 2.11, TRIPS)
    assert p.verdict == "matched" and p.confidence == "geo"
    assert p.trip is BCN and p.distance_km is not None


def test_place_geo_extends_outside_radius_in_date():
    # In the date core but ~30 km out (beyond radius+max_km, under extend_km).
    p = match.place(datetime(2024, 5, 2, 10), 41.65, 2.10, TRIPS)
    assert p.verdict == "extends" and p.confidence == "geo"
    assert p.trip is BCN


def test_place_new_when_far_and_off_date():
    p = match.place(datetime(2024, 5, 2, 10), 48.85, 2.35, TRIPS)  # Paris, ~830 km
    assert p.verdict == "new"


def test_place_date_only_when_no_gps():
    p = match.place(datetime(2024, 6, 2, 12), None, None, TRIPS)
    assert p.verdict == "matched" and p.confidence == "date-only"
    assert p.trip is TYO


def test_place_accepts_aware_datetime():
    from datetime import timezone
    aware = datetime(2024, 5, 2, 10, tzinfo=timezone.utc)
    p = match.place(aware, 41.41, 2.11, TRIPS)  # must not raise naive/aware
    assert p.verdict == "matched"


def test_place_new_when_no_date():
    p = match.place(None, 41.40, 2.10, TRIPS)
    assert p.verdict == "new" and p.confidence == "none"


def test_place_geo_beats_date_only():
    # A geo match (BCN) must outrank a date-only overlap on another trip.
    overlap = match.ExistingTrip(
        name="date-twin", source="cluster",
        start=datetime(2024, 5, 1), end=datetime(2024, 5, 3),
        lat=None, lon=None, radius_km=0.0, asset_count=5,
    )
    p = match.place(datetime(2024, 5, 2, 10), 41.41, 2.11, [overlap, BCN])
    assert p.trip is BCN and p.confidence == "geo"


# --- build_report() -------------------------------------------------------


def test_report_dedup_tally_and_excludes_dups():
    items = [
        _item("a", datetime(2024, 5, 2, 10), 41.41, 2.11),          # matched
        _item("a", datetime(2024, 5, 2, 11), 41.41, 2.11, dup="exact"),
        _item("a", datetime(2024, 5, 2, 12), 41.41, 2.11, dup="likely"),
    ]
    rep = match.build_report(items, TRIPS)
    assert rep.total_files == 3 and rep.duplicates == 2
    fr = rep.folders[0]
    assert fr.total == 3 and fr.duplicates == 2
    assert fr.placements.get("matched") == 1  # only the non-dup placed


def test_report_folder_spans_two_trips():
    items = [
        _item("mixed", datetime(2024, 5, 2, 10), 41.41, 2.11),   # BCN
        _item("mixed", datetime(2024, 6, 2, 10), 35.69, 139.70),  # TYO
    ]
    rep = match.build_report(items, TRIPS)
    fr = next(f for f in rep.folders if f.subfolder == "mixed")
    assert fr.spans_multiple and {"2024-05-barcelona", "2024-06-tokyo"} == fr.trips


def test_report_all_duplicates():
    items = [_item("a", datetime(2024, 5, 2), 41.41, 2.11, dup="exact") for _ in range(4)]
    rep = match.build_report(items, TRIPS)
    assert rep.duplicates == 4
    assert rep.folders[0].placements == {}  # nothing placed


def test_report_counts_gps_less():
    items = [
        _item("a", datetime(2024, 5, 2, 10), None, None),  # no coords
        _item("a", datetime(2024, 5, 2, 11), 41.41, 2.11),
    ]
    rep = match.build_report(items, TRIPS)
    assert rep.gps_less == 1


# --- build_existing_trips() ----------------------------------------------


def _asset(aid, when_iso, lat, lon):
    return snapshot.AssetRow(
        asset_id=aid, filename=f"{aid}.jpg", size_bytes=1, checksum=None,
        taken_at=when_iso, asset_type="IMAGE", library_id="lib",
        lat=lat, lon=lon, city=None, country=None,
    )


def test_existing_trips_album_bounds_recomputed():
    assets = [
        _asset("a1", "2024-05-01T12:00:00", 41.40, 2.10),
        _asset("a2", "2024-05-03T12:00:00", 41.41, 2.11),
    ]
    albums = [snapshot.AlbumRow(album_id="alb1", name="2024-05-barcelona",
                                marker_key="k1")]
    membership = {"alb1": {"a1", "a2"}}
    trips = match.build_existing_trips(assets, albums, membership)
    assert len(trips) == 1
    t = trips[0]
    assert t.source == "album" and t.name == "2024-05-barcelona"
    assert t.start == datetime(2024, 5, 1, 12) and t.end == datetime(2024, 5, 3, 12)
    assert t.lat is not None and t.radius_km > 0


def test_existing_trips_unmarked_album_used_by_name():
    # Albums carry no immy-cluster marker in the real library — must still be
    # used as trips, keyed by name.
    assets = [
        _asset("a1", "2024-05-01T12:00:00", 41.40, 2.10),
        _asset("a2", "2024-05-03T12:00:00", 41.41, 2.11),
    ]
    albums = [snapshot.AlbumRow("alb1", "2024-05-barcelona", marker_key=None)]
    trips = match.build_existing_trips(assets, albums, {"alb1": {"a1", "a2"}})
    assert len(trips) == 1 and trips[0].name == "2024-05-barcelona"
    assert trips[0].source == "album"


def test_existing_trips_dateless_album_dropped():
    # Members have GPS but no capture date → no usable bounds → no trip, and
    # they are NOT claimed away from raw clustering (Grok HIGH).
    assets = [_asset("a1", None, 41.40, 2.10), _asset("a2", None, 41.41, 2.11)]
    albums = [snapshot.AlbumRow("alb1", "x", marker_key=None)]
    trips = match.build_existing_trips(assets, albums, {"alb1": {"a1", "a2"}})
    assert trips == []


def test_album_date_outlier_is_fenced_out():
    # 5 assets in May 2024 + one misdated to 2016 → bounds must stay in 2024,
    # not span 8 years (which would date-match unrelated trips).
    assets = [_asset(f"a{i}", f"2024-05-0{i + 1}T12:00:00", 41.4, 2.1)
              for i in range(5)]
    assets.append(_asset("bad", "2016-02-05T00:00:00", 41.4, 2.1))
    albums = [snapshot.AlbumRow("alb1", "2024-05-barcelona", marker_key=None)]
    trips = match.build_existing_trips(
        assets, albums, {"alb1": {a.asset_id for a in assets}})
    t = trips[0]
    assert t.start.year == 2024 and t.end.year == 2024


def test_existing_trips_raw_points_cluster_the_rest():
    # 3 close-in-time-and-space assets, none in an album → one synthetic trip.
    assets = [
        _asset(f"r{i}", f"2024-07-10T1{i}:00:00", 35.68, 139.69)
        for i in range(3)
    ]
    trips = match.build_existing_trips(assets, [], {})
    assert len(trips) == 1 and trips[0].source == "cluster"


# --- snapshot v1 guard ----------------------------------------------------


def test_require_schema_rejects_old(tmp_path: Path):
    db = snapshot.create(tmp_path / "s.sqlite")
    try:
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.commit()
        with pytest.raises(RuntimeError, match="re-run"):
            snapshot.require_schema(db)
    finally:
        db.close()
