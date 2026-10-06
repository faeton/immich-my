"""`immy trips`: day track → trips → albums. Synthetic data throughout."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immy import cli
from immy import trips as T
from immy.config import load as load_config

D0 = date(2025, 3, 1)

# Rough city centres, enough for radius checks.
LISBON = (38.72, -9.14, "Portugal", "Lisbon")
PORTO = (41.15, -8.61, "Portugal", "Porto")
MADRID = (40.42, -3.70, "Spain", "Madrid")
PARIS = (48.86, 2.35, "France", "Paris")
DUBAI = (25.20, 55.27, "United Arab Emirates", "Dubai")
DOHA = (25.29, 51.53, "Qatar", "Doha")
NADI = (-17.80, 177.42, "Fiji", "Nadi")
SUVA = (-18.14, 178.44, "Fiji", "Suva")
VILA = (-17.73, 168.32, "Vanuatu", "Port Vila")
AUCKLAND = (-36.85, 174.76, "New Zealand", "Auckland")
MAHE = (-4.62, 55.45, "Seychelles", "Victoria")


def day(offset: int, place, n: int = 10) -> T.PlaceCount:
    lat, lon, country, city = place
    return T.PlaceCount(day=D0 + timedelta(days=offset), country=country,
                        city=city, lat=lat, lon=lon, n=n)


def track(*spec) -> list[T.Day]:
    """`track((0, LISBON), (1, PARIS), …)` → built days."""
    return T.build_days([day(o, p) for o, p in spec])


def run(spec, **kw) -> list[T.Trip]:
    return T.segment(track(*spec), **kw)


# --- day track ---------------------------------------------------------------


def test_country_is_majority_vote_and_city_comes_from_winning_country() -> None:
    # Five shots in Paris, three on the Madrid tarmac: France wins, and the
    # city is voted only among French buckets.
    days = T.build_days([day(0, PARIS, 5), day(0, MADRID, 3)])
    assert len(days) == 1
    assert (days[0].country, days[0].code, days[0].city) == ("France", "FR", "Paris")


def test_vote_tie_breaks_on_name_not_row_order() -> None:
    a = T.build_days([day(0, PARIS, 4), day(0, MADRID, 4)])
    b = T.build_days([day(0, MADRID, 4), day(0, PARIS, 4)])
    assert a[0].country == b[0].country == "France"


def test_unknown_country_names_are_ignored() -> None:
    bogus = T.PlaceCount(day=D0, country="Atlantis", city=None, lat=0, lon=0, n=50)
    assert T.build_days([bogus]) == []


def test_longitude_mean_survives_the_antimeridian() -> None:
    east = T.PlaceCount(day=D0, country="Fiji", city="A", lat=-17, lon=179.5, n=1)
    west = T.PlaceCount(day=D0, country="Fiji", city="B", lat=-17, lon=-179.5, n=1)
    lon = T.build_days([east, west])[0].lon
    assert abs(abs(lon) - 180) < 0.01


# --- homes -------------------------------------------------------------------


def test_home_by_radius_inside_window_only() -> None:
    home = T.HomeStay(lat=LISBON[0], lon=LISBON[1], radius_km=50, end=D0 + timedelta(days=1))
    d_in, d_far, d_late = track((0, LISBON), (1, PORTO), (2, LISBON))
    assert home.matches(d_in)
    assert not home.matches(d_far)    # Porto is ~275 km away
    assert not home.matches(d_late)   # after the window


def test_home_by_country() -> None:
    home = T.HomeStay(country="PT")
    lis, par = track((0, LISBON), (1, PARIS))
    assert home.matches(lis) and not home.matches(par)


def test_home_needs_a_place() -> None:
    assert not T.HomeStay().matches(track((0, LISBON))[0])


def test_home_days_split_trips() -> None:
    home = T.HomeStay(country="PT")
    trips = run([(0, PARIS), (1, LISBON), (2, PARIS)], homes=[home])
    assert [(t.start, t.end) for t in trips] == [(D0, D0), (D0 + timedelta(days=2),) * 2]


def test_no_homes_means_every_day_is_travel() -> None:
    trips = run([(0, LISBON), (1, LISBON), (2, LISBON)])
    assert len(trips) == 1 and trips[0].span_days == 3


# --- regions / gaps / transit --------------------------------------------------


def test_regional_countries_stay_one_trip() -> None:
    trips = run([(0, VILA), (1, VILA), (2, NADI), (3, SUVA), (4, AUCKLAND)])
    assert len(trips) == 1
    assert trips[0].region_label == "Oceania"


def test_european_countries_are_their_own_region() -> None:
    trips = run([(0, LISBON), (1, LISBON), (2, MADRID), (3, MADRID), (4, PARIS), (5, PARIS)])
    assert [t.countries()[0][1] for t in trips] == ["Portugal", "Spain", "France"]


def test_quiet_gap_longer_than_limit_splits() -> None:
    trips = run([(0, PARIS), (1, PARIS), (6, PARIS)], max_gap_days=3)
    assert len(trips) == 2
    trips = run([(0, PARIS), (1, PARIS), (5, PARIS)], max_gap_days=3)
    assert len(trips) == 1


def test_one_day_stopover_folds_into_the_trip_it_touches() -> None:
    # Spain → a day in Dubai → a week in the Seychelles.
    spec = [(0, MADRID), (1, MADRID), (2, MADRID), (3, DUBAI)]
    spec += [(4 + i, MAHE) for i in range(5)]
    trips = run(spec)
    assert len(trips) == 2
    africa = trips[1]
    assert africa.start == D0 + timedelta(days=3)
    assert [n for _, n in africa.countries()] == ["Seychelles", "UAE"]
    # The stopover is in the description but not the album name.
    assert africa.name() == "2025-03 Seychelles · Victoria"


def test_stopover_between_two_stints_rejoins_them() -> None:
    trips = run([(0, PARIS), (1, PARIS), (2, MADRID), (3, PARIS), (4, PARIS)])
    assert len(trips) == 1
    assert trips[0].name() == "2025-03 France · Paris"


def test_transit_zero_disables_folding() -> None:
    trips = run([(0, PARIS), (1, PARIS), (2, MADRID), (3, PARIS)], transit_days=0)
    assert len(trips) == 3


def test_two_day_trip_is_not_a_stopover() -> None:
    spec = [(0, PARIS), (1, PARIS), (2, DOHA), (3, DOHA), (4, PARIS), (5, PARIS)]
    trips = run(spec)
    assert [t.countries()[0][1] for t in trips] == ["France", "Qatar", "France"]


def test_region_override_from_config() -> None:
    # Make Spain and Portugal one region.
    regions = T.Regions({"ES": "Iberia", "PT": "Iberia"})
    trips = run([(0, LISBON), (1, LISBON), (2, MADRID), (3, MADRID)], regions=regions)
    assert len(trips) == 1
    assert trips[0].name() == "2025-03 Iberia · Portugal, Spain"


# --- membership / naming ---------------------------------------------------------


def test_assets_join_by_date_including_untagged_days() -> None:
    trips = run([(0, PARIS), (2, PARIS), (10, MADRID)])
    T.assign_assets(trips, [
        ("a", D0), ("drone", D0 + timedelta(days=1)), ("b", D0 + timedelta(days=2)),
        ("gap", D0 + timedelta(days=5)), ("c", D0 + timedelta(days=10)),
        ("before", D0 - timedelta(days=1)),
    ])
    assert trips[0].asset_ids == ["a", "drone", "b"]
    assert trips[1].asset_ids == ["c"]


def test_keep_drops_small_trips() -> None:
    t = run([(0, PARIS)])[0]
    T.assign_assets([t], [(f"x{i}", D0) for i in range(5)])
    assert not T.keep(t, min_assets=20)
    assert T.keep(t, min_assets=5)


def test_names() -> None:
    assert run([(0, PARIS), (1, PARIS)])[0].name() == "2025-03 France · Paris"
    # A tour across towns gets no city.
    tour = run([(0, LISBON), (1, PORTO), (2, (37.02, -7.93, "Portugal", "Faro"))])[0]
    assert tour.name() == "2025-03 Portugal"
    many = run([(0, VILA), (1, NADI), (2, AUCKLAND), (3, (-33.87, 151.21, "Australia", "Sydney")),
                (4, (-21.14, -175.2, "Tonga", "Nukuʻalofa"))])[0]
    assert many.name() == "2025-03 Oceania · Vanuatu, Fiji, New Zealand +2"


def test_parenthetical_place_qualifier_is_dropped() -> None:
    q = (34.75, 32.45, "Cyprus", "Geroskípou (quarter)")
    assert run([(0, q), (1, q)])[0].name() == "2025-03 Cyprus · Geroskípou"


def test_format_range() -> None:
    assert T.format_range(date(2024, 4, 15), date(2024, 4, 15)) == "15 Apr 2024"
    assert T.format_range(date(2024, 4, 15), date(2024, 4, 17)) == "15–17 Apr 2024"
    assert T.format_range(date(2024, 4, 29), date(2024, 5, 3)) == "29 Apr – 3 May 2024"
    assert T.format_range(date(2024, 12, 29), date(2025, 1, 3)) == "29 Dec 2024 – 3 Jan 2025"


def test_marker_roundtrip_and_tag() -> None:
    t = run([(0, PARIS), (1, PARIS)])[0]
    desc = T.description_for(t)
    assert T.extract_key(desc) == t.key()
    assert T.extract_key("my notes\nimmy-trip:abc") == "abc"
    assert T.extract_key("no marker") is None
    assert T.tag_for(t) == "Trips/2025/2025-03 France · Paris"


def test_ledger_match_by_region_and_overlap() -> None:
    t = run([(0, PARIS), (1, PARIS), (2, PARIS)])[0]
    ledger = {
        "old": {"start": (D0 - timedelta(days=1)).isoformat(),
                "end": (D0 + timedelta(days=1)).isoformat(), "region": "FR"},
        "elsewhere": {"start": D0.isoformat(), "end": D0.isoformat(), "region": "ES"},
    }
    assert T.ledger_match(t, ledger, set()) == "old"
    assert T.ledger_match(t, ledger, {"old"}) is None


# --- config ---------------------------------------------------------------------


def test_config_parses_trips_block(tmp_path: Path) -> None:
    p = tmp_path / "config.yml"
    p.write_text(
        "trips:\n"
        "  homes:\n"
        "    - name: Lisbon\n      until: 2021-06-30\n      lat: 38.72\n      lon: -9.14\n"
        "    - from: 2023-11-01\n      until: '2024-03-31'\n      country: Spain\n"
        "  max_gap_days: 5\n  min_assets: 10\n  regions:\n    TR: Middle East\n    ES: ''\n"
    )
    tc = load_config(p).trips
    assert tc is not None
    lis, es = tc.homes
    assert (lis.name, lis.end, lis.lat, lis.radius_km) == ("Lisbon", date(2021, 6, 30), 38.72, 50.0)
    assert (es.start, es.end, es.country) == (date(2023, 11, 1), date(2024, 3, 31), "ES")
    assert (tc.max_gap_days, tc.min_assets, tc.transit_days) == (5, 10, None)
    assert tc.regions == {"TR": "Middle East", "ES": ""}


@pytest.mark.parametrize("body,msg", [
    ("    - name: x\n", "needs country"),
    ("    - country: Atlantis\n", "unknown country"),
    ("    - country: PT\n      from: someday\n", "YYYY-MM-DD"),
])
def test_config_rejects_bad_homes(tmp_path: Path, body: str, msg: str) -> None:
    p = tmp_path / "config.yml"
    p.write_text("trips:\n  homes:\n" + body)
    with pytest.raises(ValueError, match=msg):
        load_config(p)


# --- CLI ------------------------------------------------------------------------


class _Cursor:
    def __init__(self, buckets, assets):
        self.buckets, self.assets, self._rows = buckets, assets, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if 'FROM "user"' in sql:
            self._rows = [("u1", "me@example.com")]
        elif "GROUP BY 1, 2, 3" in sql:
            assert params["placeholder_min"] == 10
            self._rows = self.buckets
        else:
            self._rows = [r if len(r) == 3 else (*r, False) for r in self.assets]

    def fetchall(self):
        return self._rows

    def executemany(self, sql, params):
        verb = sql.strip().split()[0]
        for p in params:
            LINKS.append((verb, p["asset"], p.get("tag"), p["value"]))
            if verb == "INSERT":
                TAGGED.add((p["asset"], p["value"]))
            elif verb == "DELETE":
                TAGGED.discard((p["asset"], p["value"]))


LINKS: list[tuple] = []
TAGGED: set[tuple[str, str]] = set()   # (asset, tag value) currently linked


class _Conn:
    def __init__(self, buckets, assets):
        self.c = _Cursor(buckets, assets)

    def cursor(self):
        return self.c

    def commit(self):
        pass

    def close(self):
        pass


class _Immich:
    albums: dict[str, dict] = {}
    tags: dict[str, set] = {}

    def __init__(self, **kw):
        pass

    def _request(self, method, path, body=None):
        assert (method, path) == ("GET", "/api/albums")
        return [{"id": k, "albumName": v["name"], "description": v["description"]}
                for k, v in self.albums.items()]

    def create_album(self, name, *, description, asset_ids):
        aid = f"al{len(self.albums)}"
        self.albums[aid] = {"name": name, "description": description, "assets": set(asset_ids)}
        return aid

    def update_album(self, album_id, *, description):
        self.albums[album_id]["description"] = description

    def add_assets_to_album(self, album_id, ids):
        have = self.albums[album_id]["assets"]
        out = [{"id": i, "success": i not in have} for i in ids]
        have.update(ids)
        return out

    def remove_assets_from_album(self, album_id, ids):
        have = self.albums[album_id]["assets"]
        out = [{"id": i, "success": i in have} for i in ids]
        have.difference_update(ids)
        return out

    def upsert_tags(self, names):
        for n in names:
            self.tags.setdefault(n, set())
        return {n: n for n in names}

    def tag_assets(self, tag_id, ids):
        self.tags[tag_id].update(ids)
        return [{"id": i, "success": True} for i in ids]


def _setup(monkeypatch, tmp_path, buckets, assets):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        f"state_root: {tmp_path / 'state'}\n"
        "pg: {host: h, port: 1, user: u, password: p, database: d}\n"
        "immich: {url: http://x, api_key: k, library_id: l}\n"
        "trips:\n  min_assets: 2\n  homes:\n    - country: PT\n"
    )
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: _Conn(buckets, assets))
    _Immich.albums, _Immich.tags = {}, {}
    LINKS.clear()
    TAGGED.clear()
    monkeypatch.setattr(cli, "ImmichClient", _Immich)
    return cfg


def _rows(spec):
    return [(b.day, b.country, b.city, b.lat, b.lon, b.n) for b in (day(o, p) for o, p in spec)]


def test_cli_dry_run_writes_nothing(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [(f"a{i}", D0 + timedelta(days=i)) for i in range(4)]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    out = tmp_path / "trips.csv"
    res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg), "--csv", str(out)])
    assert res.exit_code == 0, res.output
    assert "2025-03 France · Paris" in res.output
    assert "dry-run" in res.output
    assert _Immich.albums == {}
    assert not (tmp_path / "state").exists()
    assert "2025-03 France · Paris" in out.read_text()


def test_cli_apply_is_idempotent_and_follows_a_moved_start(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (2, PARIS), (3, PARIS), (4, LISBON)])
    assets = [("a0", D0), ("a2", D0 + timedelta(days=2)), ("a3", D0 + timedelta(days=3))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    r = CliRunner()
    res = r.invoke(cli.app, ["trips", "--config", str(cfg), "--apply", "--tags"])
    assert res.exit_code == 0, res.output
    assert len(_Immich.albums) == 1
    album = next(iter(_Immich.albums.values()))
    assert album["assets"] == {"a2", "a3"}
    # Tags are created by API, linked and locked by SQL, never via tag_assets.
    assert set(_Immich.tags) == {"Trips/2025/2025-03 France · Paris"}
    assert _Immich.tags["Trips/2025/2025-03 France · Paris"] == set()
    tag = "Trips/2025/2025-03 France · Paris"
    assert sorted(LINKS) == sorted(
        [(verb, a, tag, tag) for verb in ("UPDATE", "INSERT") for a in ("a2", "a3")])

    # The user renames the album and adds a note above the marker.
    album["name"] = "Paris with friends"
    album["description"] = "Best croissants.\n" + album["description"].splitlines()[-1]

    # A late import adds a Paris day earlier: the trip's key changes, but it
    # must land in the same album, keep the note, and prune the day-3 asset
    # that no longer belongs.
    buckets2 = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON), (4, LISBON)])
    assets2 = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2)),
               ("a3", D0 + timedelta(days=3))]
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: _Conn(buckets2, assets2))
    res = r.invoke(cli.app, ["trips", "--config", str(cfg), "--apply", "--prune"])
    assert res.exit_code == 0, res.output
    assert len(_Immich.albums) == 1
    assert album["name"] == "Paris with friends"
    assert album["assets"] == {"a1", "a2"}
    lines = album["description"].splitlines()
    assert lines[0] == "Best croissants."
    assert T.extract_key(album["description"]) == T.stable_key("FR", D0 + timedelta(days=1))
    ledger = T.load_ledger(tmp_path / "state" / T.LEDGER_FILENAME)
    assert list(ledger) == [T.stable_key("FR", D0 + timedelta(days=1))]


def test_cli_requires_owner_with_several_users(monkeypatch, tmp_path) -> None:
    cfg = _setup(monkeypatch, tmp_path, [], [])

    class Multi(_Cursor):
        def execute(self, sql, params=None):
            self._rows = [("u1", "a@x"), ("u2", "b@x")]

    monkeypatch.setattr(cli.pg_mod, "connect",
                        lambda _cfg: type("C", (), {"cursor": lambda s: Multi([], []),
                                                    "close": lambda s: None})())
    res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg)])
    assert res.exit_code == 2
    assert "--owner" in res.output


# --- legs ------------------------------------------------------------------------

TONGA = (-21.14, -175.2, "Tonga", "Nukuʻalofa")
GIBRALTAR = (36.14, -5.35, "Gibraltar", "Gibraltar")


def _pacific() -> T.Trip:
    # Tonga 0–2, (photo-less 3), Fiji 4, Vanuatu 5–6, NZ 7–8, Vanuatu 9 (blip), NZ 10.
    spec = [(0, TONGA), (1, TONGA), (2, TONGA), (4, NADI), (5, VILA), (6, VILA),
            (7, AUCKLAND), (8, AUCKLAND), (9, VILA), (10, AUCKLAND)]
    trips = run(spec)
    assert len(trips) == 1
    return trips[0]


def test_legs_cover_every_day_in_order() -> None:
    legs = _pacific().legs()
    assert [(l.country, l.start.day, l.end.day) for l in legs] == [
        ("Tonga", 1, 4),        # the photo-less day 3 stays with Tonga
        ("Fiji", 5, 5),
        ("Vanuatu", 6, 7),
        ("New Zealand", 8, 11),  # the one-day Vanuatu blip doesn't split it
    ]


def test_territory_counts_as_its_country_in_legs() -> None:
    t = run([(0, MADRID), (1, GIBRALTAR), (2, MADRID), (3, GIBRALTAR), (4, GIBRALTAR)])[0]
    assert [(l.code, l.country) for l in t.legs()] == [("ES", "Spain")]


def test_description_lists_the_itinerary() -> None:
    t = _pacific()
    lines = T.description_for(t).splitlines()
    assert lines[0] == "1–11 Mar 2025 · 11 days · 4 countries"
    assert lines[1:5] == ["Tonga · 1–4 Mar", "Fiji · 5 Mar", "Vanuatu · 6–7 Mar",
                          "New Zealand · 8–11 Mar"]
    assert T.extract_key("\n".join(lines)) == t.key()


def test_one_country_description_has_no_itinerary() -> None:
    lines = T.description_for(run([(0, PARIS), (1, PARIS)])[0]).splitlines()
    assert lines[0] == "1–2 Mar 2025 · 2 days · France"
    assert len(lines) == 2


def test_leg_tags_nest_under_the_trip_and_split_assets_by_date() -> None:
    t = _pacific()
    T.assign_assets([t], [(f"d{i}", D0 + timedelta(days=i)) for i in range(11)])
    pairs = T.leg_tags(t)
    base = "Trips/2025/2025-03 Oceania · Tonga, Vanuatu, New Zealand +1"
    assert T.tag_for(t) == base
    assert [name for _, name in pairs] == [
        f"{base}/Tonga · 1–4 Mar 2025", f"{base}/Fiji · 5 Mar 2025",
        f"{base}/Vanuatu · 6–7 Mar 2025", f"{base}/New Zealand · 8–11 Mar 2025",
    ]
    split = T.assets_by_leg(t, {f"d{i}": D0 + timedelta(days=i) for i in range(11)})
    assert [ids for _, ids in split] == [
        ["d0", "d1", "d2", "d3"], ["d4"], ["d5", "d6"], ["d7", "d8", "d9", "d10"],
    ]


def test_single_country_trip_tags_at_trip_level() -> None:
    t = run([(0, PARIS), (1, PARIS)])[0]
    assert [name for _, name in T.leg_tags(t)] == ["Trips/2025/2025-03 France · Paris"]


def test_tag_segments_never_contain_a_slash() -> None:
    odd = (48.86, 2.35, "France", "A/B")
    t = run([(0, odd), (1, odd)])[0]
    assert T.tag_for(t) == "Trips/2025/2025-03 France · A-B"


def test_cli_skips_placeholder_dated_assets(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1), False), ("a2", D0 + timedelta(days=2), False),
              ("fake", D0 + timedelta(days=1), True)]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    out = tmp_path / "t.csv"
    res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg), "--csv", str(out)])
    assert res.exit_code == 0, res.output
    assert "+1 with a placeholder date" in " ".join(res.output.split())
    row = out.read_text().splitlines()[1].split(",")
    assert row[6] == "2"   # assets column: the placeholder-dated one is out


# --- reconciliation: ownership, disappeared trips, stale tags ---------------


def _run(cfg, *extra):
    res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg), "--apply", *extra])
    assert res.exit_code == 0, res.output
    return res


def _use(monkeypatch, buckets, assets):
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: _Conn(buckets, assets))


def test_prune_never_removes_an_asset_the_user_added(monkeypatch, tmp_path) -> None:
    # Paris trip, days 1-2; "mine" is shot on day 2.
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("mine", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg)
    album = next(iter(_Immich.albums.values()))
    # Pretend "mine" was added by hand first: reset ownership to a1 only.
    ledger_path = tmp_path / "state" / T.LEDGER_FILENAME
    ledger = T.load_ledger(ledger_path)
    (key,) = ledger
    ledger[key]["assets"] = ["a1"]
    T.save_ledger(ledger_path, ledger)
    # Re-run: "mine" is already there → not claimed.
    _run(cfg)
    assert T.load_ledger(ledger_path)[key]["assets"] == ["a1"]
    # Day 2 stops being Paris; the trip shrinks to day 1. Prune keeps "mine".
    _use(monkeypatch, _rows([(0, LISBON), (1, PARIS), (2, LISBON), (3, LISBON)]), assets)
    _run(cfg, "--prune")
    assert "mine" in album["assets"]


def test_disappeared_trip_is_retired_only_with_prune(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    album = next(iter(_Immich.albums.values()))
    album["assets"].add("by-hand")
    # Paris becomes home: the trip is gone.
    cfg.write_text(cfg.read_text().replace("    - country: PT\n", "    - country: PT\n    - country: FR\n"))
    res = _run(cfg, "--tags")
    assert "no longer found" in " ".join(res.output.split())
    assert album["assets"] == {"a1", "a2", "by-hand"}       # nothing without --prune
    res = _run(cfg, "--tags", "--prune")
    assert album["assets"] == {"by-hand"}                   # only immy's links go
    assert TAGGED == set()                                  # and its tags
    assert len(_Immich.albums) == 1                         # the album stays
    assert T.load_ledger(tmp_path / "state" / T.LEDGER_FILENAME) == {}


def test_orphans_outside_the_scope_are_left_alone(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg)
    _use(monkeypatch, _rows([(0, LISBON)]), [])
    _run(cfg, "--prune", "--since", "2026-01-01")
    album = next(iter(_Immich.albums.values()))
    assert album["assets"] == {"a1", "a2"}


def test_moved_asset_loses_its_old_trip_tag_with_prune(monkeypatch, tmp_path) -> None:
    # Paris days 1-2, Madrid days 4-5; "x" starts on day 2 (Paris).
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON), (4, MADRID), (5, MADRID)])
    assets = [("p", D0 + timedelta(days=1)), ("p2", D0 + timedelta(days=1)),
              ("x", D0 + timedelta(days=2)), ("m", D0 + timedelta(days=4))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    paris = "Trips/2025/2025-03 France · Paris"
    madrid = "Trips/2025/2025-03 Spain · Madrid"
    assert ("x", paris) in TAGGED
    # A date fix moves "x" to day 5: it now belongs to Madrid.
    moved = [("p", D0 + timedelta(days=1)), ("p2", D0 + timedelta(days=1)),
             ("x", D0 + timedelta(days=5)), ("m", D0 + timedelta(days=4))]
    _use(monkeypatch, buckets, moved)
    _run(cfg, "--tags")
    assert ("x", paris) in TAGGED and ("x", madrid) in TAGGED   # add-only without --prune
    _run(cfg, "--tags", "--prune")
    assert ("x", paris) not in TAGGED and ("x", madrid) in TAGGED
    assert ("p", paris) in TAGGED
    drops = [l for l in LINKS if l[0] == "UPDATE" and l[1:] == ("x", None, paris)]
    assert drops  # the value left the locked asset_exif.tags list too


def test_backfill_owned_tags_picks_the_trips_own_tag() -> None:
    rows = [("a", "Trips/2025/2025-12 Portugal/Lisbon · 1–2 Dec 2025"),
            ("b", "Trips/2025/2025-12 Portugal/Porto · 3 Dec 2025"),
            ("c", "Trips/2025/2025-12 Portugal"),
            ("c", "Trips/2025/2025-12 Poland · Warsaw")]   # c moved; Warsaw's own

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def execute(self, sql, p): assert p["root"] == "Trips/%"
        def fetchall(self): return rows

    conn = type("C", (), {"cursor": lambda self: Cur()})()
    got = T.backfill_owned_tags(conn, "u1", ["a", "b", "c"], "Trips")
    assert got == {rows[0], rows[1], rows[2]}
    assert T.backfill_owned_tags(conn, "u1", [], "Trips") == set()


def test_generated_lines_are_recognised() -> None:
    t = _pacific()
    for line in T.description_for(t).splitlines():
        assert T.is_generated_line(line), line
    for line in ("2 Oct – 11 Dec 2025 · 71 days · 6 countries",
                 "Australia · 22 Nov – 11 Dec", "Fiji · 8–9 Oct", "Tonga · 5 Mar"):
        assert T.is_generated_line(line), line
    for line in ("Best croissants.", "with Anna and Max", "Day 3: whales!"):
        assert not T.is_generated_line(line), line


def test_unedited_description_follows_the_trip_edited_one_stays(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg)
    album = next(iter(_Immich.albums.values()))
    assert album["description"].startswith("2–3 Mar 2025 · 2 days")
    # The trip grows by a day: an unedited description follows it.
    longer = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, PARIS), (4, LISBON)])
    _use(monkeypatch, longer, assets)
    _run(cfg)
    assert album["description"].startswith("2–4 Mar 2025 · 3 days")
    # The user edits it: later changes leave it alone…
    album["description"] = "Best croissants.\n" + album["description"]
    _use(monkeypatch, _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)]), assets)
    _run(cfg)
    assert album["description"].startswith("Best croissants.\n2–4 Mar 2025")
    # …unless forced, which keeps the user's own line.
    _run(cfg, "--refresh-descriptions")
    assert album["description"].startswith("Best croissants.\n2–3 Mar 2025 · 2 days")
    assert T.extract_key(album["description"])
