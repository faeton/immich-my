"""Tests for GPX/JSON track sidecars (`immy/track.py`) and the
WritablePaths resolvers that place them."""

from __future__ import annotations

from pathlib import Path

from immy import srt, track
from immy.paths import resolve_writable_paths
from immy.rules.geotag_from_gpx import _parse_gpx

FIXTURES = Path(__file__).parent / "fixtures" / "dji-srt-pair"


def test_gpx_roundtrips_and_drops_null_island(tmp_path: Path):
    frames = srt.parse_track(FIXTURES / "DJI_MULTI.SRT")
    dest = tmp_path / "t.gpx"
    track.write_gpx(frames, dest, name="DJI_MULTI")
    pts = _parse_gpx(dest)
    # Only the two real fixes — the (0,0) prelock frame is excluded.
    assert len(pts) == 2
    assert pts[0][1:] == (41.385100, 2.173400)
    assert pts[1][1:] == (41.385900, 2.174100)
    # <ele> carries abs_alt; the file is valid GPX 1.1.
    body = dest.read_text()
    assert "<ele>75.060</ele>" in body
    assert 'xmlns="http://www.topografix.com/GPX/1/1"' in body


def test_json_summary_and_track(tmp_path: Path):
    frames = srt.parse_track(FIXTURES / "DJI_MULTI.SRT")
    dest = tmp_path / "t.track.json"
    track.write_json(frames, dest)
    import json
    data = json.loads(dest.read_text())
    s = data["summary"]
    assert s["frames"] == 3
    assert s["fixes"] == 2
    assert s["takeoff"] == {"latitude": 41.385100, "longitude": 2.173400}
    assert s["rel_alt_max"] == 31.700
    assert s["abs_alt_max"] == 94.460
    assert len(data["track"]) == 3
    # Settings preserved per-frame.
    assert data["track"][1]["shutter"] == "1/1000.0"


def test_writable_paths_mac_default_sits_beside_media(tmp_path: Path):
    media = tmp_path / "trip" / "DJI_0001.MP4"
    wp = resolve_writable_paths(tmp_path / "trip")  # no roots → Mac layout
    assert wp.gpx_path(media) == tmp_path / "trip" / "DJI_0001.gpx"
    assert wp.track_json_path(media) == tmp_path / "trip" / "DJI_0001.track.json"


def test_writable_paths_nas_mirrors_under_sidecars_root(tmp_path: Path):
    originals = tmp_path / "originals"
    trip = originals / "2024-trip"
    media = trip / "sub" / "DJI_0001.MP4"
    sidecars = tmp_path / "sidecars"
    wp = resolve_writable_paths(
        trip, originals_root=originals,
        state_root=tmp_path / "state", sidecars_root=sidecars,
    )
    # Mirrors the media's path under the trip, never under :ro originals.
    assert wp.gpx_path(media) == sidecars / "2024-trip" / "sub" / "DJI_0001.gpx"
    assert wp.track_json_path(media) == (
        sidecars / "2024-trip" / "sub" / "DJI_0001.track.json"
    )
