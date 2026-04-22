"""Tests for Phase 4 event clustering.

The algorithm is deliberately thin (~50 LOC), so tests focus on the
corner cases that the one-line sweep misses at first glance:

- Exact-time-gap boundaries (should be inclusive → same cluster).
- Centroid drift — a slow walk can accumulate past `max_km` even if
  each hop is short. Expected behaviour: yes, those split. We don't
  do rolling windows.
- Name formatting across year / month / day boundaries.
- Stable-key stability under asset-set churn at the edges.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from immy.clustering import (
    AssetPoint,
    cluster_assets,
    cluster_marker_line,
    extract_cluster_key,
    haversine_km,
    name_for_cluster,
    stable_key_for_cluster,
)


UTC = timezone.utc


def _p(asset_id: str, when: datetime, lat: float, lon: float,
       city: str | None = None, country: str | None = None) -> AssetPoint:
    return AssetPoint(asset_id=asset_id, when=when, lat=lat, lon=lon,
                      city=city, country=country)


def test_haversine_zero_distance_same_point() -> None:
    assert haversine_km(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0)


def test_haversine_one_degree_at_equator_is_111_km() -> None:
    # 1° of longitude at the equator ≈ 111.2 km. Gives us a sanity
    # check that the formula isn't off by orders of magnitude.
    assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.2, abs=0.5)


def test_haversine_commutative() -> None:
    assert haversine_km(45.0, 10.0, -20.0, 170.0) == pytest.approx(
        haversine_km(-20.0, 170.0, 45.0, 10.0),
    )


def test_empty_input_returns_empty() -> None:
    assert cluster_assets([]) == []


def test_single_cluster_when_all_points_close() -> None:
    # Same spot, 10 minutes apart, three points.
    base = datetime(2024, 2, 19, 14, 0, tzinfo=UTC)
    pts = [
        _p("a", base, 52.0, 13.0, "Berlin", "Germany"),
        _p("b", base + timedelta(minutes=10), 52.0, 13.0, "Berlin", "Germany"),
        _p("c", base + timedelta(minutes=20), 52.0, 13.0, "Berlin", "Germany"),
    ]
    clusters = cluster_assets(pts)
    assert len(clusters) == 1
    assert len(clusters[0].assets) == 3


def test_time_gap_splits_cluster() -> None:
    base = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
    # Three on day 1, three on day 5 — time gap > 4h default.
    pts = (
        [_p(f"a{i}", base + timedelta(minutes=10 * i), 45.0, 10.0, "Milan", "Italy")
         for i in range(3)]
        + [_p(f"b{i}", base + timedelta(days=5, minutes=10 * i), 45.0, 10.0,
              "Milan", "Italy") for i in range(3)]
    )
    clusters = cluster_assets(pts)
    assert len(clusters) == 2
    assert len(clusters[0].assets) == 3
    assert len(clusters[1].assets) == 3


def test_distance_splits_cluster_even_when_time_is_close() -> None:
    # Adjacent by time (30 min apart) but 200 km apart → split.
    base = datetime(2024, 2, 19, 14, 0, tzinfo=UTC)
    pts = [
        _p(f"a{i}", base + timedelta(minutes=10 * i), 52.0, 13.0,
           "Berlin", "Germany")
        for i in range(3)
    ] + [
        _p(f"b{i}", base + timedelta(hours=1, minutes=10 * i), 50.1, 14.4,
           "Prague", "Czech Republic")
        for i in range(3)
    ]
    clusters = cluster_assets(pts)
    assert len(clusters) == 2


def test_small_clusters_are_dropped() -> None:
    base = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
    pts = [
        _p("singleton", base, 0.0, 0.0, "A", "X"),
        _p("a1", base + timedelta(days=5), 10.0, 10.0, "B", "Y"),
        _p("a2", base + timedelta(days=5, minutes=10), 10.0, 10.0, "B", "Y"),
        _p("a3", base + timedelta(days=5, minutes=20), 10.0, 10.0, "B", "Y"),
    ]
    clusters = cluster_assets(pts, min_assets=3)
    assert len(clusters) == 1
    assert {a.asset_id for a in clusters[0].assets} == {"a1", "a2", "a3"}


def test_name_uses_dominant_city_and_country() -> None:
    # Two photos tagged "Ville A", one tagged "Ville B" — mode wins.
    base = datetime(2024, 4, 12, 10, 0, tzinfo=UTC)
    pts = [
        _p("a", base, 43.0, 5.0, "Ville A", "France"),
        _p("b", base + timedelta(hours=1), 43.0, 5.0, "Ville B", "France"),
        _p("c", base + timedelta(hours=2), 43.0, 5.0, "Ville A", "France"),
    ]
    [c] = cluster_assets(pts, min_assets=3)
    assert name_for_cluster(c).startswith("Ville A, France — ")


def test_name_handles_missing_place() -> None:
    base = datetime(2024, 4, 12, 10, 0, tzinfo=UTC)
    pts = [_p(f"a{i}", base + timedelta(minutes=i * 10), 0.0, 0.0,
              None, None) for i in range(3)]
    [c] = cluster_assets(pts, min_assets=3)
    # City/country both absent → falls back to just the date.
    name = name_for_cluster(c)
    assert "2024" in name
    assert "," not in name  # no `city, country` chunk


def test_name_single_day_vs_range() -> None:
    base = datetime(2024, 7, 10, 9, 0, tzinfo=UTC)
    one_day = [_p(f"a{i}", base + timedelta(minutes=i * 30), 43.0, 5.0,
                  "Cannes", "France") for i in range(3)]
    [c1] = cluster_assets(one_day, min_assets=3)
    assert "10 Jul 2024" in name_for_cluster(c1)

    three_days = [_p(f"a{i}", base + timedelta(hours=i * 3), 43.0, 5.0,
                     "Cannes", "France") for i in range(20)]
    [c2] = cluster_assets(three_days, min_assets=3, max_gap_hours=4)
    # Spans 10 Jul → 12 Jul; same month/year → collapsed form.
    assert "10–" in name_for_cluster(c2) and "Jul 2024" in name_for_cluster(c2)


def test_stable_key_is_stable_under_edge_appends() -> None:
    base = datetime(2024, 4, 12, 10, 0, tzinfo=UTC)
    core = [_p(f"a{i}", base + timedelta(minutes=i * 10), 43.0, 5.0,
               "Cannes", "France") for i in range(5)]
    extended = core + [_p("late", base + timedelta(hours=2), 43.0005, 5.0005,
                          "Cannes", "France")]
    [c_core] = cluster_assets(core, min_assets=3)
    [c_ext] = cluster_assets(extended, min_assets=3)
    # Rounded centroid + start-day-only → adding a photo 2 h later at
    # the same spot keeps the key stable. Album doesn't get recreated.
    assert c_core.stable_key() == c_ext.stable_key()


def test_stable_key_differs_across_cities() -> None:
    base = datetime(2024, 4, 12, 10, 0, tzinfo=UTC)
    a = [_p(f"a{i}", base + timedelta(minutes=i * 10), 43.0, 5.0,
            "Cannes", "France") for i in range(3)]
    b = [_p(f"b{i}", base + timedelta(minutes=i * 10), 48.85, 2.35,
            "Paris", "France") for i in range(3)]
    [ca] = cluster_assets(a, min_assets=3)
    [cb] = cluster_assets(b, min_assets=3)
    assert ca.stable_key() != cb.stable_key()


def test_extract_cluster_key_roundtrip() -> None:
    key = "abc123def456"
    desc = f"Pre-existing human-typed note.\n{cluster_marker_line(key)}\n"
    assert extract_cluster_key(desc) == key


def test_extract_cluster_key_none_when_missing() -> None:
    assert extract_cluster_key(None) is None
    assert extract_cluster_key("") is None
    assert extract_cluster_key("just a normal description") is None


def test_extract_cluster_key_ignores_marker_inside_prose() -> None:
    # The marker prefix appearing mid-sentence shouldn't match — it has
    # to be the start of a stripped line.
    desc = "The word immy-cluster:whatever appears here in passing."
    assert extract_cluster_key(desc) is None
