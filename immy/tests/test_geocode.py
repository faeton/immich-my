from __future__ import annotations

from pathlib import Path

import yaml

from immy.rules import geocode_place
from immy.rules.geocode_place import _propose


def _write_notes(folder: Path, fm: dict) -> Path:
    notes = folder / "TRIP.md"
    notes.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n")
    return notes


def test_fires_when_name_set_and_coords_missing(tmp_path: Path, monkeypatch):
    folder = tmp_path / "trip"
    folder.mkdir()
    _write_notes(folder, {"trip": "Safari", "location": {"name": "Casela, Mauritius"}})
    monkeypatch.setattr(geocode_place, "_query_nominatim", lambda q: (-20.2594, 57.3823))
    monkeypatch.setattr(geocode_place, "CACHE_PATH", tmp_path / "places.yml")
    findings = _propose([], folder)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "geocode-place"
    assert f.action == "write_notes"
    assert f.confidence == "high"
    assert f.patch["location_coords"] == [-20.2594, 57.3823]


def test_skips_when_coords_already_set(tmp_path: Path, monkeypatch):
    folder = tmp_path / "trip"
    folder.mkdir()
    _write_notes(folder, {
        "location": {"name": "Casela, Mauritius", "coords": [-20.25, 57.38]},
    })
    # If this fires Nominatim, the test blows up (no network mock).
    monkeypatch.setattr(geocode_place, "_query_nominatim",
                        lambda q: (_ for _ in ()).throw(AssertionError("must not call")))
    monkeypatch.setattr(geocode_place, "CACHE_PATH", tmp_path / "places.yml")
    assert _propose([], folder) == []


def test_skips_when_no_name(tmp_path: Path, monkeypatch):
    folder = tmp_path / "trip"
    folder.mkdir()
    _write_notes(folder, {"location": {"coords": None, "name": None}})
    monkeypatch.setattr(geocode_place, "CACHE_PATH", tmp_path / "places.yml")
    assert _propose([], folder) == []


def test_silent_offline(tmp_path: Path, monkeypatch):
    folder = tmp_path / "trip"
    folder.mkdir()
    _write_notes(folder, {"location": {"name": "Nowhere-Specific"}})
    monkeypatch.setattr(geocode_place, "_query_nominatim", lambda q: None)
    monkeypatch.setattr(geocode_place, "CACHE_PATH", tmp_path / "places.yml")
    assert _propose([], folder) == []


def test_uses_cache_without_network(tmp_path: Path, monkeypatch):
    folder = tmp_path / "trip"
    folder.mkdir()
    _write_notes(folder, {"location": {"name": "Casela, Mauritius"}})
    cache = tmp_path / "places.yml"
    cache.write_text(yaml.safe_dump({"Casela, Mauritius": [-20.1, 57.2]}))
    monkeypatch.setattr(geocode_place, "CACHE_PATH", cache)
    monkeypatch.setattr(geocode_place, "_query_nominatim",
                        lambda q: (_ for _ in ()).throw(AssertionError("must not call")))
    findings = _propose([], folder)
    assert len(findings) == 1
    assert findings[0].patch["location_coords"] == [-20.1, 57.2]


def test_persists_cache_after_network_hit(tmp_path: Path, monkeypatch):
    folder = tmp_path / "trip"
    folder.mkdir()
    _write_notes(folder, {"location": {"name": "Somewhere, Earth"}})
    cache = tmp_path / "places.yml"
    monkeypatch.setattr(geocode_place, "CACHE_PATH", cache)
    monkeypatch.setattr(geocode_place, "_query_nominatim", lambda q: (1.0, 2.0))
    _propose([], folder)
    assert cache.is_file()
    loaded = yaml.safe_load(cache.read_text())
    assert loaded == {"Somewhere, Earth": [1.0, 2.0]}
