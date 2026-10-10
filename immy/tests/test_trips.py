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


@pytest.fixture(autouse=True)
def _no_live_schema(no_schema_guard):
    """These CLI fakes can't answer the schema guard's information_schema
    queries; the guard itself is tested in test_schema_contract.py."""


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
            self._rows = list(USERS)
        elif "GROUP BY 1, 2, 3" in sql:
            assert params["placeholder_min"] == 10
            self._rows = self.buckets
        elif sql is T.OTHER_OWNERS_SQL:
            self._rows = [(a, ASSET_OWNER.get(a, "u1") == params["owner"]) for a in params["ids"]]
        elif sql is T.LINK_TAGS_RETURNING_SQL:
            # The fake's tag ids are the tag names (see _Immich.upsert_tags).
            self._rows = []
            for a, v in zip(params["assets"], params["tags"]):
                LINKS.append(("INSERT", a, v, v))
                if (a, v) not in TAGGED:
                    TAGGED.add((a, v))
                    self._rows.append((a, v))
        elif sql is T.LINKS_BY_VALUE_SQL:
            self._rows = [(a, v) for a, v in TAGGED
                          if a in params["assets"] and v in params["values"]]
        elif sql is T.OWNED_TAGS_SQL:
            root = params["root"].rstrip("%")
            self._rows = [(a, v) for a, v in TAGGED
                          if a in params["assets"] and v.startswith(root)]
        else:
            self._rows = [r if len(r) == 3 else (*r, False) for r in self.assets]

    def fetchall(self):
        return self._rows

    def executemany(self, sql, params):
        verb = sql.strip().split()[0]
        for p in params:
            if sql is T.DROP_TAG_VALUE_SQL and ASSET_OWNER.get(p["asset"], "u1") != p["owner"]:
                continue
            LINKS.append((verb, p["asset"], p.get("tag"), p["value"]))
            if verb == "INSERT":
                TAGGED.add((p["asset"], p["value"]))
            elif verb == "DELETE" and ASSET_OWNER.get(p["asset"], "u1") == p["owner"]:
                TAGGED.discard((p["asset"], p["value"]))


USERS: list[tuple[str, str]] = [("u1", "me@example.com")]
ASSET_OWNER: dict[str, str] = {}   # asset → owner id; default u1
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

    failing_tags: set[str] = set()

    def upsert_tags(self, names):
        for n in names:
            self.tags.setdefault(n, set())
        return {n: n for n in names if n not in self.failing_tags}

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
    _Immich.albums, _Immich.tags, _Immich.failing_tags = {}, {}, set()
    LINKS.clear()
    TAGGED.clear()
    ASSET_OWNER.clear()
    USERS[:] = [("u1", "me@example.com")]
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
    ledger = T.load_ledger(tmp_path / "state" / T.LEDGER_FILENAME, "u1")
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
    ledger = T.load_ledger(ledger_path, "u1")
    (key,) = ledger
    ledger[key]["assets"] = ["a1"]
    T.save_ledger(ledger_path, ledger, "u1")
    # Re-run: "mine" is already there → not claimed.
    _run(cfg)
    assert T.load_ledger(ledger_path, "u1")[key]["assets"] == ["a1"]
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
    assert T.load_ledger(tmp_path / "state" / T.LEDGER_FILENAME, "u1") == {}


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


# --- reconciliation: the second review's cases ----------------------------

PARIS_TAG = "Trips/2025/2025-03 France · Paris"


def _ledger(tmp_path, owner="u1"):
    return T.load_ledger(tmp_path / "state" / T.LEDGER_FILENAME, owner)


def test_ledger_is_kept_per_owner_and_a_run_never_retires_anothers(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    USERS[:] = [("u1", "alice@x"), ("u2", "bob@x")]
    _run(cfg, "--owner", "alice@x", "--tags")
    alice = _ledger(tmp_path)
    assert alice and TAGGED == {("a1", PARIS_TAG), ("a2", PARIS_TAG)}
    # Bob has no trips at all: nothing of Alice's is an orphan to him.
    _use(monkeypatch, [], [])
    res = _run(cfg, "--owner", "bob@x", "--tags", "--prune")
    assert "retired" not in res.output
    assert _ledger(tmp_path) == alice
    assert _ledger(tmp_path, "u2") == {}
    assert TAGGED == {("a1", PARIS_TAG), ("a2", PARIS_TAG)}
    assert next(iter(_Immich.albums.values()))["assets"] == {"a1", "a2"}


def test_drop_tag_value_is_limited_to_the_owner() -> None:
    assert '"ownerId" = %(owner)s' in T.DROP_TAG_VALUE_SQL


def _write_legacy(tmp_path, trips):
    path = tmp_path / "state" / T.LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(__import__("json").dumps({"schema": 1, "trips": trips}))
    return path


def test_schema_1_entries_move_to_the_user_whose_assets_they_are(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    USERS[:] = [("u1", "alice@x"), ("u2", "bob@x")]
    ASSET_OWNER.update({"b1": "u2"})
    key = T.stable_key("FR", D0 + timedelta(days=1))
    path = _write_legacy(tmp_path, {
        key: {"start": "2025-03-02", "end": "2025-03-03", "region": "FR",
              "assets": ["a1", "a2"], "tags": {}},
        "bobs": {"start": "2024-01-01", "end": "2024-01-02", "region": "ES",
                 "assets": ["b1"], "tags": {}},
    })
    _run(cfg, "--owner", "alice@x", "--prune")
    assert key in _ledger(tmp_path)
    assert "bobs" not in _ledger(tmp_path)                   # never retired by Alice
    assert "bobs" in T.load_legacy(path)                     # still waiting for Bob
    _use(monkeypatch, [], [])
    _run(cfg, "--owner", "bob@x")
    assert "bobs" in _ledger(tmp_path, "u2") and not T.load_legacy(path)


def test_a_trip_whose_start_moved_out_of_scope_is_not_retired(monkeypatch, tmp_path) -> None:
    # Paris days 2-3 (Mar 3-4); a late import adds Paris on day 1 (Mar 2).
    buckets = _rows([(0, LISBON), (2, PARIS), (3, PARIS), (4, LISBON)])
    assets = [("a2", D0 + timedelta(days=2)), ("a3", D0 + timedelta(days=3))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    buckets2 = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, PARIS), (4, LISBON)])
    _use(monkeypatch, buckets2, assets + [("a1", D0 + timedelta(days=1))])
    res = _run(cfg, "--tags", "--prune", "--since", "2025-03-03")
    assert "retired" not in res.output
    assert next(iter(_Immich.albums.values()))["assets"] == {"a2", "a3"}
    assert {("a2", PARIS_TAG), ("a3", PARIS_TAG)} <= TAGGED
    (entry,) = _ledger(tmp_path).values()
    assert entry["assets"] == ["a2", "a3"]


def test_a_tag_value_two_trips_share_survives_the_old_ones_retirement(monkeypatch, tmp_path) -> None:
    # Two Paris trips in March: days 1-2 and days 5-6, home in between.
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON), (4, LISBON),
                     (5, PARIS), (6, PARIS), (7, LISBON)])
    assets = [("p1", D0 + timedelta(days=1)), ("x", D0 + timedelta(days=2)),
              ("q1", D0 + timedelta(days=5)), ("q2", D0 + timedelta(days=6))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    assert len(_ledger(tmp_path)) == 2
    # The first trip disappears (Lisbon after all) and "x" moves to the second.
    buckets2 = _rows([(0, LISBON), (1, LISBON), (2, LISBON), (3, LISBON), (4, LISBON),
                      (5, PARIS), (6, PARIS), (7, LISBON)])
    moved = [("p1", D0 + timedelta(days=1)), ("x", D0 + timedelta(days=6)),
             ("q1", D0 + timedelta(days=5)), ("q2", D0 + timedelta(days=6))]
    _use(monkeypatch, buckets2, moved)
    res = _run(cfg, "--tags", "--prune")
    assert "retired" in res.output
    assert ("x", PARIS_TAG) in TAGGED                       # the second trip's now
    assert ("p1", PARIS_TAG) not in TAGGED
    (entry,) = _ledger(tmp_path).values()
    assert sorted(entry["tags"][PARIS_TAG]) == ["q1", "q2", "x"]


def test_legacy_tag_ownership_survives_a_run_without_tags(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    path = tmp_path / "state" / T.LEDGER_FILENAME
    ledger = _ledger(tmp_path)
    (key,) = ledger
    del ledger[key]["tags"]                                  # written before tags were tracked
    T.save_ledger(path, ledger, "u1")
    _run(cfg)                                                # no --tags
    assert sorted(_ledger(tmp_path)[key]["tags"][PARIS_TAG]) == ["a1", "a2"]
    # And retirement of an untracked entry still removes its tags.
    ledger = _ledger(tmp_path)
    del ledger[key]["tags"]
    T.save_ledger(path, ledger, "u1")
    cfg.write_text(cfg.read_text().replace("    - country: PT\n", "    - country: PT\n    - country: FR\n"))
    _run(cfg, "--prune")
    assert TAGGED == set()


def test_prune_without_tags_still_removes_stale_trip_tags(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("b1", D0 + timedelta(days=1)),
              ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    _use(monkeypatch, buckets, assets[:2] + [("a2", D0 + timedelta(days=3))])
    _run(cfg, "--prune")
    assert TAGGED == {("a1", PARIS_TAG), ("b1", PARIS_TAG)}


def test_a_tag_assigned_by_hand_is_never_claimed(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("mine", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    TAGGED.add(("mine", PARIS_TAG))                          # the user got there first
    _run(cfg, "--tags")
    assert _ledger(tmp_path)[T.stable_key("FR", D0 + timedelta(days=1))]["tags"] == {
        PARIS_TAG: ["a1"]}
    _use(monkeypatch, buckets, [("a1", D0 + timedelta(days=1)), ("mine", D0 + timedelta(days=3))])
    _run(cfg, "--tags", "--prune")
    assert ("mine", PARIS_TAG) in TAGGED


def test_a_failed_tag_upsert_never_prunes_a_wanted_tag(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    _run(cfg, "--tags")
    _Immich.failing_tags = {PARIS_TAG}
    res = _run(cfg, "--tags", "--prune")
    assert "tag upsert failed" in res.output
    assert TAGGED == {("a1", PARIS_TAG), ("a2", PARIS_TAG)}
    (entry,) = _ledger(tmp_path).values()
    assert sorted(entry["tags"][PARIS_TAG]) == ["a1", "a2"]


def test_a_shared_tag_moving_into_an_out_of_scope_trip_is_kept(monkeypatch, tmp_path) -> None:
    # Paris on days 1-2 (Mar 2-3) and days 19-20 (Mar 20-21).
    spec = [(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON), (18, LISBON),
            (19, PARIS), (20, PARIS), (21, LISBON)]
    assets = [("p1", D0 + timedelta(days=1)), ("p2", D0 + timedelta(days=2)),
              ("x", D0 + timedelta(days=19)), ("q", D0 + timedelta(days=20))]
    cfg = _setup(monkeypatch, tmp_path, _rows(spec), assets)
    _run(cfg, "--tags")
    early = T.stable_key("FR", D0 + timedelta(days=1))
    # The later trip turns out to be Lisbon; "x" was really shot on day 2.
    spec2 = [(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON), (18, LISBON),
             (19, LISBON), (20, LISBON), (21, LISBON)]
    moved = [("p1", D0 + timedelta(days=1)), ("p2", D0 + timedelta(days=2)),
             ("x", D0 + timedelta(days=2)), ("q", D0 + timedelta(days=20))]
    _use(monkeypatch, _rows(spec2), moved)
    res = _run(cfg, "--tags", "--prune", "--since", "2025-03-15")
    assert "retired" in res.output
    assert ("x", PARIS_TAG) in TAGGED and ("q", PARIS_TAG) not in TAGGED
    assert "x" in _ledger(tmp_path)[early]["tags"][PARIS_TAG]   # handed over


def test_a_second_apply_run_is_refused_while_one_holds_the_ledger(monkeypatch, tmp_path) -> None:
    cfg = _setup(monkeypatch, tmp_path, [], [])
    held = T.lock_ledger(tmp_path / "state" / T.LEDGER_FILENAME)
    try:
        res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg), "--apply"])
        assert res.exit_code == 2 and "holds" in res.output
        # A dry run only reads, so it still works.
        assert CliRunner().invoke(cli.app, ["trips", "--config", str(cfg)]).exit_code == 0
    finally:
        held.close()
    _run(cfg)


def test_tag_links_commit_only_after_the_ledger_records_them(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)
    seen = []
    monkeypatch.setattr(_Conn, "commit", lambda self: seen.append(
        _ledger(tmp_path).get(T.stable_key("FR", D0 + timedelta(days=1)), {}).get("pending_tags")))
    _run(cfg, "--tags")
    assert seen and seen[0] == {PARIS_TAG: ["a1", "a2"]}
    (entry,) = _ledger(tmp_path).values()
    assert entry["tags"] == {PARIS_TAG: ["a1", "a2"]} and "pending_tags" not in entry


def test_a_shared_tag_kept_for_a_trip_without_an_entry_stays_owned(monkeypatch, tmp_path) -> None:
    # Only the later Paris trip exists at first; "x" belongs to it.
    spec = [(0, LISBON), (1, LISBON), (2, LISBON), (3, LISBON), (18, LISBON),
            (19, PARIS), (20, PARIS), (21, LISBON)]
    assets = [("p1", D0 + timedelta(days=1)), ("p2", D0 + timedelta(days=2)),
              ("x", D0 + timedelta(days=19)), ("q", D0 + timedelta(days=20))]
    cfg = _setup(monkeypatch, tmp_path, _rows(spec), assets)
    _run(cfg, "--tags")
    late = T.stable_key("FR", D0 + timedelta(days=19))
    # The later one turns out Lisbon; an early Paris trip appears, with "x".
    spec2 = [(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON), (18, LISBON),
             (19, LISBON), (20, LISBON), (21, LISBON)]
    moved = [("p1", D0 + timedelta(days=1)), ("p2", D0 + timedelta(days=2)),
             ("x", D0 + timedelta(days=2)), ("q", D0 + timedelta(days=20))]
    _use(monkeypatch, _rows(spec2), moved)
    _run(cfg, "--tags", "--prune", "--since", "2025-03-15")
    assert ("x", PARIS_TAG) in TAGGED and ("q", PARIS_TAG) not in TAGGED
    assert _ledger(tmp_path)[late]["tags"] == {PARIS_TAG: ["x"]}   # still immy's
    # A full run reaches the early trip: it takes "x" over, the old entry goes.
    _run(cfg, "--tags", "--prune")
    early = T.stable_key("FR", D0 + timedelta(days=1))
    assert set(_ledger(tmp_path)) == {early}
    assert sorted(_ledger(tmp_path)[early]["tags"][PARIS_TAG]) == ["p1", "p2", "x"]
    # And it is still prunable as immy's: the early trip goes too.
    cfg.write_text(cfg.read_text().replace("    - country: PT\n", "    - country: PT\n    - country: FR\n"))
    _run(cfg, "--prune")
    assert TAGGED == set()


def test_links_whose_commit_was_lost_are_settled_on_the_next_run(monkeypatch, tmp_path) -> None:
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)

    def crash(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(_Conn, "commit", crash)
    res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg), "--apply", "--tags"])
    assert res.exit_code != 0
    del res                      # the crashed run's frame holds the ledger lock;
    __import__("gc").collect()   # a real process releases it on exit
    (entry,) = _ledger(tmp_path).values()
    assert entry["pending_tags"] == {PARIS_TAG: ["a1", "a2"]} and entry["tags"] == {}
    # The commit was lost for a2 only (a1 made it).
    TAGGED.discard(("a2", PARIS_TAG))
    monkeypatch.setattr(_Conn, "commit", lambda self: None)
    _run(cfg)
    (entry,) = _ledger(tmp_path).values()
    assert "pending_tags" not in entry
    assert entry["tags"] == {PARIS_TAG: ["a1"]}


@pytest.mark.parametrize("old,new,code", [
    ("Netherlands", "The Netherlands", "NL"),
    ("Lao People's Democratic Republic", "Laos", "LA"),
    ("Holy See (Vatican City State)", "Vatican", "VA"),
    ("State of Palestine", "Palestinian Territory", "PS"),
    ("Moldova, Republic of", "Moldova", "MD"),
    ("United States of America", "United States", "US"),
    ("Czech Republic", "Czechia", "CZ"),
])
def test_country_names_before_and_after_immich_3_3(old, new, code) -> None:
    assert T.country_code(old) == code
    assert T.country_code(new) == code


def test_every_immich_3_3_country_name_resolves() -> None:
    import json
    rows = json.loads((T._DATA / "geonames_countries.json").read_text())["countries"]
    unresolved = [name for a2, _, name in rows if T.country_code(name) != a2]
    assert unresolved == []


def test_a_synonym_two_countries_share_resolves_to_neither() -> None:
    assert T.country_code("Congo") is None
    assert T.country_code("Republic of the Congo") == "CG"


def test_display_names_follow_the_code_not_immichs_string() -> None:
    assert T.short_country("NL", "The Netherlands") == "Netherlands"
    assert T.short_country("TR", "Turkey") == "Türkiye"
    assert T.short_country("FR", "anything") == "France"
    assert T.short_country("CV", "Cabo Verde") == "Cape Verde"      # the pre-3.3 name
    assert T.short_country("XK", "Kosovo") == "Kosovo"              # GeoNames only
    assert T.short_country("ZZ", "Nowhere") == "Nowhere"


def test_apply_refuses_to_write_on_a_drifted_schema(monkeypatch, tmp_path) -> None:
    from immy import schema_contract
    buckets = _rows([(0, LISBON), (1, PARIS), (2, PARIS), (3, LISBON)])
    assets = [("a1", D0 + timedelta(days=1)), ("a2", D0 + timedelta(days=2))]
    cfg = _setup(monkeypatch, tmp_path, buckets, assets)

    def drifted(conn):
        raise schema_contract.SchemaMismatch("person: missing read columns: personGroupId")
    monkeypatch.setattr(schema_contract, "assert_live_schema", drifted)
    res = CliRunner().invoke(cli.app, ["trips", "--config", str(cfg), "--apply", "--tags"])
    assert res.exit_code == 2 and "personGroupId" in res.output
    assert _Immich.albums == {} and TAGGED == set()
    # A dry run only reads: still fine.
    assert CliRunner().invoke(cli.app, ["trips", "--config", str(cfg)]).exit_code == 0


def test_a_day_split_between_two_names_of_one_country_votes_as_one() -> None:
    d = date(2025, 3, 1)
    days = T.build_days([
        T.PlaceCount(d, "Netherlands", "Amsterdam", 52.37, 4.9, 4),
        T.PlaceCount(d, "The Netherlands", "Amsterdam", 52.37, 4.9, 4),
        T.PlaceCount(d, "Belgium", "Brussels", 50.85, 4.35, 6),
    ])
    assert days[0].code == "NL" and days[0].n == 8 and days[0].city == "Amsterdam"
