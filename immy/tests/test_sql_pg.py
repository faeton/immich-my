"""SQL-level tests against a real Postgres: the queries whose logic lives in
SQL (sign-flipped GPS, the antimeridian, placeholder dates, durable tag
links). Mocks can't exercise these.

Needs a throwaway Postgres, never the live Immich DB:

    scripts/test-pg.sh up
    IMMY_TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/postgres \\
        uv run --no-sync pytest tests/test_sql_pg.py

Skipped when IMMY_TEST_PG_DSN is unset. Each test gets its own schema.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from immy import trips as T  # noqa: E402

DSN = os.environ.get("IMMY_TEST_PG_DSN")
pytestmark = [
    pytest.mark.scratch_pg,
    pytest.mark.skipif(not DSN, reason="IMMY_TEST_PG_DSN not set (scripts/test-pg.sh up)"),
]

OWNER = str(uuid.uuid4())
OTHER = str(uuid.uuid4())

# The columns these queries read/write, with Immich 3.0.2's types.
SCHEMA = """
CREATE TABLE asset (
  id uuid PRIMARY KEY, "ownerId" uuid NOT NULL, "deletedAt" timestamptz,
  visibility varchar NOT NULL DEFAULT 'timeline', "localDateTime" timestamptz NOT NULL
);
CREATE TABLE asset_exif (
  "assetId" uuid PRIMARY KEY REFERENCES asset(id), latitude double precision,
  longitude double precision, country varchar, city varchar,
  tags varchar[], "lockedProperties" varchar[]
);
CREATE TABLE tag (id uuid PRIMARY KEY, "userId" uuid NOT NULL, value varchar NOT NULL);
CREATE TABLE tag_asset (
  "assetId" uuid NOT NULL, "tagId" uuid NOT NULL, PRIMARY KEY ("assetId", "tagId")
);
-- Immich 3.3 people: faces point at a person group; each user who sees
-- the person has a row keyed by (owner, group).
CREATE TABLE person_group (id uuid PRIMARY KEY);
CREATE TABLE asset_face (
  id uuid PRIMARY KEY, "assetId" uuid NOT NULL REFERENCES asset(id),
  "personGroupId" uuid REFERENCES person_group(id), "deletedAt" timestamptz,
  "imageWidth" int NOT NULL DEFAULT 100, "imageHeight" int NOT NULL DEFAULT 100,
  "boundingBoxX1" int NOT NULL DEFAULT 10, "boundingBoxY1" int NOT NULL DEFAULT 10,
  "boundingBoxX2" int NOT NULL DEFAULT 20, "boundingBoxY2" int NOT NULL DEFAULT 20
);
CREATE TABLE person (
  "ownerId" uuid NOT NULL, "personGroupId" uuid NOT NULL REFERENCES person_group(id),
  name varchar NOT NULL DEFAULT '', "faceAssetId" uuid REFERENCES asset_face(id),
  PRIMARY KEY ("ownerId", "personGroupId")
);
"""


@pytest.fixture
def conn():
    """A private schema per test, so parallel workers (-n auto) never share
    tables; dropped afterwards."""
    c = psycopg.connect(DSN, autocommit=True)
    schema = f"immy_test_{uuid.uuid4().hex[:12]}"
    c.execute(f"CREATE SCHEMA {schema}")
    c.execute(f"SET search_path TO {schema}")
    c.execute(SCHEMA)
    # Immich's DB session runs a non-UTC zone; local-day maths must not care.
    c.execute("SET TIME ZONE 'Europe/Lisbon'")
    try:
        yield c
    finally:
        c.execute(f"DROP SCHEMA {schema} CASCADE")
        c.close()


def add(conn, when: datetime, lat=None, lon=None, country=None, city=None,
        *, owner=OWNER, visibility="timeline", deleted=False) -> str:
    """`when` is the shot's wall clock; Immich stores it tagged as UTC."""
    aid = str(uuid.uuid4())
    conn.execute(
        'INSERT INTO asset (id, "ownerId", "deletedAt", visibility, "localDateTime") '
        "VALUES (%s, %s, %s, %s, %s)",
        (aid, owner, datetime.now(timezone.utc) if deleted else None, visibility,
         when.replace(tzinfo=timezone.utc)),
    )
    conn.execute(
        'INSERT INTO asset_exif ("assetId", latitude, longitude, country, city) '
        "VALUES (%s, %s, %s, %s, %s)", (aid, lat, lon, country, city))
    return aid


def buckets(conn, placeholder_min=10):
    rows = conn.execute(T.DAY_BUCKETS_SQL, {"owner": OWNER, "placeholder_min": placeholder_min}).fetchall()
    return [T.PlaceCount(r[0], r[1], r[2], float(r[3]), float(r[4]), int(r[5])) for r in rows]


def countries_by_day(conn, **kw) -> dict[date, str]:
    return {d.day: d.country for d in T.build_days(buckets(conn, **kw))}


# --- sign-flipped GPS ------------------------------------------------------------


def test_full_flip_ghosts_are_dropped_on_mixed_and_fully_flipped_days(conn) -> None:
    # An Antarctic cruise: real points near (-64.8, -62.9). Day 1 mixes real
    # and flipped (64.8, 62.9) points; day 2 is entirely flipped, with no
    # mirror of its own; day 3 is clean.
    for h in range(6):
        add(conn, datetime(2024, 3, 6, 9 + h), -64.8, -62.9, "Antarctica")
    for h in range(4):
        add(conn, datetime(2024, 3, 6, 10 + h), 64.8, 62.9, "Russian Federation", "Ovgort")
    for h in range(8):
        add(conn, datetime(2024, 3, 7, 9 + h), 64.81, 62.91, "Russian Federation", "Ovgort")
    for h in range(5):
        add(conn, datetime(2024, 3, 8, 9 + h), -64.7, -62.8, "Antarctica")
    by_day = countries_by_day(conn)
    assert by_day[date(2024, 3, 6)] == "Antarctica"
    assert date(2024, 3, 7) not in by_day          # all ghosts, nothing real left
    assert by_day[date(2024, 3, 8)] == "Antarctica"


def test_longitude_only_flip_is_dropped(conn) -> None:
    # Chicago (41.88, -87.63) with lon-flipped twins in Xinjiang (41.88, 87.63).
    for h in range(6):
        add(conn, datetime(2024, 2, 9, 9 + h), 41.88, -87.63, "United States of America", "Chicago")
    for h in range(3):
        add(conn, datetime(2024, 2, 9, 10 + h), 41.88, 87.63, "People's Republic of China")
    assert countries_by_day(conn)[date(2024, 2, 9)] == "United States of America"
    assert all(b.country != "People's Republic of China" for b in buckets(conn))


def test_a_real_trip_between_hemispheres_is_not_a_ghost(conn) -> None:
    # Buenos Aires one day, Madrid the next: different hemispheres, but not
    # mirror images of each other, so nothing is dropped.
    for h in range(4):
        add(conn, datetime(2024, 3, 15, 9 + h), -34.6, -58.4, "Argentina")
        add(conn, datetime(2024, 3, 16, 9 + h), 40.4, -3.7, "Spain")
    by_day = countries_by_day(conn)
    assert by_day == {date(2024, 3, 15): "Argentina", date(2024, 3, 16): "Spain"}


def test_greenwich_day_trip_is_not_a_lon_flip(conn) -> None:
    # Within 5° of Greenwich a lon "mirror" is just Europe: London (-0.12) and
    # a point at +0.12 the same day are both real.
    add(conn, datetime(2024, 6, 1, 9), 51.5, -0.12, "United Kingdom")
    add(conn, datetime(2024, 6, 1, 15), 51.5, 0.12, "United Kingdom")
    assert sum(b.n for b in buckets(conn)) == 2


def test_null_island_deleted_and_hidden_are_ignored(conn) -> None:
    add(conn, datetime(2024, 1, 1, 9), 0.0, 0.0, "Atlantis-ish")
    add(conn, datetime(2024, 1, 2, 9), 48.85, 2.35, "France", deleted=True)
    add(conn, datetime(2024, 1, 3, 9), 48.85, 2.35, "France", visibility="hidden")
    add(conn, datetime(2024, 1, 4, 9), 48.85, 2.35, "France", owner=OTHER)
    assert buckets(conn) == []


# --- antimeridian -----------------------------------------------------------------


def test_bucket_longitude_is_a_circular_mean(conn) -> None:
    add(conn, datetime(2025, 10, 8, 9), -17.0, 179.5, "Fiji", "Nadi")
    add(conn, datetime(2025, 10, 8, 10), -17.0, -179.5, "Fiji", "Nadi")
    (b,) = buckets(conn)
    assert abs(abs(b.lon) - 180.0) < 1e-6
    # And a radius home there still matches.
    home = T.HomeStay(lat=-17.0, lon=180.0, radius_km=50)
    assert home.matches(T.build_days([b])[0])


# --- local day --------------------------------------------------------------------


def test_local_day_is_the_wall_clock_day_in_any_session_zone(conn) -> None:
    # 23:30 local wall clock stays on its own day although the session runs
    # Europe/Lisbon (+01:00 in summer would push it to the next day).
    add(conn, datetime(2024, 7, 1, 23, 30), 48.85, 2.35, "France")
    assert list(countries_by_day(conn)) == [date(2024, 7, 1)]
    (row,) = conn.execute(T.ASSETS_SQL, {"owner": OWNER, "placeholder_min": 10}).fetchall()
    assert row[1] == date(2024, 7, 1)


# --- placeholder dates ------------------------------------------------------------


def test_placeholder_stamp_is_flagged_and_kept_out_of_the_day_track(conn) -> None:
    stamp = datetime(2019, 1, 1, 12, 0, 0)
    fakes = [add(conn, stamp, 41.88, -87.63, "United States of America") for _ in range(12)]
    real = add(conn, datetime(2019, 1, 1, 12, 0, 1), 48.85, 2.35, "France")
    rows = {str(r[0]): r[2] for r in conn.execute(
        T.ASSETS_SQL, {"owner": OWNER, "placeholder_min": 10}).fetchall()}
    assert all(rows[a] for a in fakes)
    assert rows[real] is False
    assert {b.country for b in buckets(conn)} == {"France"}


def test_below_threshold_or_off_the_hour_is_not_a_placeholder(conn) -> None:
    few = [add(conn, datetime(2010, 1, 1, 12), 50.4, 30.5, "Ukraine") for _ in range(3)]
    burst = [add(conn, datetime(2015, 8, 14, 15, 0, 7), 45.0, 7.0, "Italy") for _ in range(20)]
    rows = {str(r[0]): r[2] for r in conn.execute(
        T.ASSETS_SQL, {"owner": OWNER, "placeholder_min": 10}).fetchall()}
    assert not any(rows[a] for a in few + burst)


def test_on_the_hour_burst_of_the_threshold_is_flagged(conn) -> None:
    """Documented limit: 10+ shots sharing an exact whole-hour second are
    taken for a placeholder. Real bursts land on :00:00 rarely enough."""
    burst = [add(conn, datetime(2015, 8, 14, 15, 0, 0), 45.0, 7.0, "Italy") for _ in range(10)]
    rows = {str(r[0]): r[2] for r in conn.execute(
        T.ASSETS_SQL, {"owner": OWNER, "placeholder_min": 10}).fetchall()}
    assert all(rows[a] for a in burst)


# --- durable tag links --------------------------------------------------------------


def _tag(conn, value, owner=OWNER) -> str:
    tid = str(uuid.uuid4())
    conn.execute('INSERT INTO tag (id, "userId", value) VALUES (%s, %s, %s)', (tid, owner, value))
    return tid


def _state(conn, aid):
    tags, locked = conn.execute(
        'SELECT tags, "lockedProperties" FROM asset_exif WHERE "assetId" = %s', (aid,)).fetchone()
    linked = {v for (v,) in conn.execute(
        'SELECT t.value FROM tag_asset ta JOIN tag t ON t.id = ta."tagId" WHERE ta."assetId" = %s',
        (aid,)).fetchall()}
    return set(tags or []), set(locked or []), linked


def test_link_tags_links_appends_and_locks_idempotently(conn) -> None:
    aid = add(conn, datetime(2025, 10, 3, 9), -21.1, -175.2, "Tonga")
    conn.execute("""UPDATE asset_exif SET tags = ARRAY['Gear/Camera/X5'], "lockedProperties" = ARRAY['description']
                    WHERE "assetId" = %s""", (aid,))
    leg = "Trips/2025/2025-10 Oceania/Tonga · 2–7 Oct 2025"
    tid = _tag(conn, leg)
    for _ in range(2):  # re-running changes nothing
        T.link_tags(conn, [(aid, tid, leg)])
    tags, locked, linked = _state(conn, aid)
    assert tags == {"Gear/Camera/X5", leg}
    assert locked == {"description", "tags"}
    assert linked == {leg}


def test_unlink_tags_drops_only_that_value_and_keeps_the_lock(conn) -> None:
    aid = add(conn, datetime(2025, 10, 3, 9), -21.1, -175.2, "Tonga")
    old, new, gear = "Trips/2025/old", "Trips/2025/new", "Gear/Camera/X5"
    ids = {v: _tag(conn, v) for v in (old, new, gear)}
    T.link_tags(conn, [(aid, ids[v], v) for v in (old, new, gear)])
    # Another user's tag with the same value must not be touched.
    other_tid = _tag(conn, old, owner=OTHER)
    conn.execute('INSERT INTO tag_asset ("assetId", "tagId") VALUES (%s, %s)', (aid, other_tid))
    T.unlink_tags(conn, OWNER, [(aid, old)])
    tags, locked, linked = _state(conn, aid)
    assert tags == {new, gear}
    assert "tags" in locked
    assert linked == {new, gear, old}   # the remaining `old` link is OTHER's tag
    assert conn.execute('SELECT count(*) FROM tag_asset WHERE "tagId" = %s', (ids[old],)).fetchone()[0] == 0


def test_backfill_owned_tags_reads_the_trips_own_links(conn) -> None:
    a = add(conn, datetime(2025, 12, 19, 9), 38.7, -9.1, "Portugal")
    b = add(conn, datetime(2025, 12, 18, 9), 52.2, 21.0, "Poland")
    pt, pl, gear = "Trips/2025/2025-12 Portugal", "Trips/2025/2025-12 Poland · Warsaw", "Gear/X"
    ids = {v: _tag(conn, v) for v in (pt, pl, gear)}
    T.link_tags(conn, [(a, ids[pt], pt), (b, ids[pt], pt), (b, ids[pl], pl), (a, ids[gear], gear)])
    got = T.backfill_owned_tags(conn, OWNER, [a, b], "Trips")
    assert got == {(a, pt), (b, pt)}


def test_link_tags_reports_only_the_links_it_created(conn) -> None:
    mine = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France")
    hand = add(conn, datetime(2025, 3, 2, 10), 48.86, 2.35, "France")
    v = "Trips/2025/2025-03 France · Paris"
    tid = _tag(conn, v)
    conn.execute('INSERT INTO tag_asset ("assetId", "tagId") VALUES (%s, %s)', (hand, tid))
    created = T.link_tags(conn, [(mine, tid, v), (hand, tid, v), (mine, tid, v)], report=True)
    assert created == {(mine, v)}
    assert T.link_tags(conn, [(mine, tid, v)], report=True) == set()
    assert _state(conn, hand)[2] == {v}


def test_unlink_tags_never_touches_another_owners_asset(conn) -> None:
    theirs = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France", owner=OTHER)
    v = "Trips/2025/2025-03 France · Paris"
    their_tid = _tag(conn, v, owner=OTHER)
    T.link_tags(conn, [(theirs, their_tid, v)])
    T.unlink_tags(conn, OWNER, [(theirs, v)])
    tags, _, linked = _state(conn, theirs)
    assert tags == {v} and linked == {v}


def test_adopt_legacy_splits_entries_by_asset_owner(conn) -> None:
    a = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France")
    b = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France", owner=OTHER)
    gone = str(uuid.uuid4())   # deleted since: belongs to no one
    legacy = {
        "mine": {"assets": [a, gone], "tags": {}},
        "theirs": {"assets": [b], "tags": {}},
        "mixed": {"assets": [a], "tags": {"x": [b]}},
        "empty": {"assets": [], "tags": {}},
    }
    adopted, rest = T.adopt_legacy(conn, OWNER, legacy, sole_user=False)
    assert set(adopted) == {"mine"} and set(rest) == {"theirs", "mixed", "empty"}
    adopted, _ = T.adopt_legacy(conn, OWNER, legacy, sole_user=True)
    assert set(adopted) == {"mine", "empty"}


def test_uncommitted_links_vanish_on_rollback(conn) -> None:
    conn.autocommit = False
    aid = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France")
    v = "Trips/x"
    tid = _tag(conn, v)
    conn.commit()
    assert T.link_tags(conn, [(aid, tid, v)], report=True, commit=False) == {(aid, v)}
    conn.rollback()
    assert _state(conn, aid) == (set(), set(), set())
    conn.rollback()
    conn.autocommit = True


def test_confirm_pending_keeps_only_links_that_exist(conn) -> None:
    a = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France")
    b = add(conn, datetime(2025, 3, 2, 10), 48.86, 2.35, "France")
    v = "Trips/2025/2025-03 France · Paris"
    tid = _tag(conn, v)
    T.link_tags(conn, [(a, tid, v)])
    entry = {"tags": {}, "pending_tags": {v: [a, b]}}
    assert T.confirm_pending(conn, OWNER, entry)
    assert entry == {"tags": {v: [a]}}
    assert not T.confirm_pending(conn, OWNER, entry)


def test_relinking_leaves_an_up_to_date_row_untouched(conn) -> None:
    aid = add(conn, datetime(2025, 3, 2, 9), 48.86, 2.35, "France")
    v = "Trips/x"
    tid = _tag(conn, v)
    T.link_tags(conn, [(aid, tid, v)])
    xmin = conn.execute('SELECT xmin::text FROM asset_exif WHERE "assetId" = %s', (aid,)).fetchone()
    T.link_tags(conn, [(aid, tid, v)])
    assert conn.execute('SELECT xmin::text FROM asset_exif WHERE "assetId" = %s', (aid,)).fetchone() == xmin



# --- people (Immich 3.3: person groups) ------------------------------------------


def _face(conn, asset, group=None) -> str:
    fid = str(uuid.uuid4())
    conn.execute('INSERT INTO asset_face (id, "assetId", "personGroupId") VALUES (%s, %s, %s)',
                 (fid, asset, group))
    return fid


def _group(conn, *owners, name="", feature=None) -> str:
    gid = str(uuid.uuid4())
    conn.execute("INSERT INTO person_group (id) VALUES (%s)", (gid,))
    for o in owners:
        conn.execute('INSERT INTO person ("ownerId", "personGroupId", name, "faceAssetId") '
                     "VALUES (%s, %s, %s, %s)", (o, gid, name, feature))
    return gid


def _names(conn, gid):
    return dict(conn.execute('SELECT "ownerId"::text, name FROM person WHERE "personGroupId" = %s',
                             (gid,)).fetchall())


def test_people_are_named_and_seen_per_owner(conn) -> None:
    from immy import pg
    a = add(conn, datetime(2025, 3, 2, 9))
    gid = _group(conn, OWNER, OTHER)                 # shared: both users see it
    f = _face(conn, a, gid)
    conn.execute('UPDATE person SET "faceAssetId" = %s', (f,))
    assert pg.name_person(conn, gid, "Anya", OWNER) == "named"
    assert _names(conn, gid) == {OWNER: "Anya", OTHER: ""}   # only the owner's row
    assert pg.name_person(conn, gid, "Anya", OWNER) == "already"
    assert pg.name_person(conn, gid, "Bob", OWNER) is None   # never overwritten
    faces = pg.fetch_existing_faces(conn, [a], OWNER)[a]
    assert [(x[1], x[2], x[7], x[8]) for x in faces] == [(gid, "Anya", True, True)]
    # The other user sees the same face as theirs, unnamed — but only their own
    # assets are listed, and this asset is OWNER's.
    assert pg.fetch_existing_faces(conn, [a], OTHER) == {}


def test_a_group_without_the_owners_row_is_not_unnamed(conn) -> None:
    from immy import pg
    a = add(conn, datetime(2025, 3, 2, 9))
    gid = _group(conn, OTHER)                         # only the other user has a row
    _face(conn, a, gid)
    (face,) = pg.fetch_existing_faces(conn, [a], OWNER)[a]
    assert face[1] == gid and face[2] is None and face[7] is False
    assert pg.name_person(conn, gid, "Anya", OWNER) is None
    assert _names(conn, gid) == {OTHER: ""}


def test_orphans_attach_only_to_the_named_person_with_a_feature_face(conn) -> None:
    from immy import pg
    a = add(conn, datetime(2025, 3, 2, 9))
    gid = _group(conn, OWNER)
    feat = _face(conn, a, gid)
    orphan = _face(conn, a)
    # No feature face yet: nothing attaches.
    pg.name_person(conn, gid, "Anya", OWNER)
    assert pg.attach_orphan_faces(conn, [orphan], gid, owner_id=OWNER, name="Anya") == 0
    conn.execute('UPDATE person SET "faceAssetId" = %s', (feat,))
    # Renamed since the preview: nothing attaches either.
    assert pg.attach_orphan_faces(conn, [orphan], gid, owner_id=OWNER, name="Bob") == 0
    assert pg.attach_orphan_faces(conn, [orphan], gid, owner_id=OWNER, name="Anya") == 1
    # Another user's asset's face is never attached.
    b = add(conn, datetime(2025, 3, 2, 9), owner=OTHER)
    theirs = _face(conn, b)
    assert pg.attach_orphan_faces(conn, [theirs], gid, owner_id=OWNER, name="Anya") == 0


# --- districts → city -------------------------------------------------------------

_GEO = """
SELECT pg_advisory_lock(4242);
CREATE EXTENSION IF NOT EXISTS cube SCHEMA public;
CREATE EXTENSION IF NOT EXISTS earthdistance SCHEMA public;
CREATE OR REPLACE FUNCTION public.ll_to_earth_public(latitude double precision, longitude double precision)
 RETURNS public.earth LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $f$
  SELECT public.cube(public.cube(public.cube(public.earth()*cos(radians(latitude))*cos(radians(longitude))),
         public.earth()*cos(radians(latitude))*sin(radians(longitude))),
         public.earth()*sin(radians(latitude)))::public.earth $f$;
SELECT pg_advisory_unlock(4242);
SELECT set_config('search_path', current_setting('search_path') || ', public', false);
CREATE TABLE geodata_places (
  id int PRIMARY KEY, name varchar NOT NULL, latitude float8 NOT NULL, longitude float8 NOT NULL,
  "countryCode" char(2) NOT NULL, "admin1Name" varchar, "admin2Name" varchar, "alternateNames" varchar
);
"""

# Immich's geodata (GeoNames cities500): name, lat, lon, cc, admin1, admin2, alternates.
_PLACES = [
    ("Warsaw", 52.2298, 21.0118, "PL", "Mazovia", "Warszawa", "Varsovie,Warszawa"),
    ("Mokotów", 52.1934, 21.0346, "PL", "Mazovia", "Warszawa", None),
    ("Bielany", 52.2924, 20.9353, "PL", "Mazovia", "Warszawa", None),
    ("Bielany", 52.3417, 22.2493, "PL", "Mazovia", "Powiat sokołowski", None),
    ("Murcia", 37.9870, -1.1300, "ES", "Murcia", "Murcia", None),
    ("Santiago de la Ribera", 37.7967, -0.8085, "ES", "Murcia", "Murcia", None),
    ("London", 51.5085, -0.1257, "GB", "England", "Greater London", None),
    ("City of London", 51.5128, -0.0918, "GB", "England", "City of London", "London"),
    ("Highbury", 51.5520, -0.0970, "GB", "England", "Greater London", None),
    ("Kyiv", 50.4547, 30.5238, "UA", "Kyiv City", None, "Kiev"),
    ("Zhulyany", 50.4017, 30.4469, "UA", "Kyiv City", None, None),
]


def test_city_parents_rolls_districts_up_and_leaves_towns_alone(conn) -> None:
    conn.execute(_GEO)
    for i, row in enumerate(_PLACES):
        conn.execute('INSERT INTO geodata_places VALUES (%s, %s, %s, %s, %s, %s, %s, %s)', (i, *row))
    d = date(2025, 3, 1)
    b = [T.PlaceCount(d, "Poland", "Mokotów", 52.19, 21.03, 5),
         T.PlaceCount(d, "Poland", "Bielany", 52.29, 20.94, 5),       # the Warsaw one
         T.PlaceCount(d, "Spain", "Santiago de la Ribera", 37.80, -0.81, 5),
         T.PlaceCount(d, "United Kingdom", "Highbury", 51.55, -0.10, 5),
         T.PlaceCount(d, "Ukraine", "Zhulyany", 50.40, 30.45, 5),
         T.PlaceCount(d, "Poland", "Warsaw", 52.23, 21.01, 5)]
    got = {k[:2]: v for k, v in T.city_parents(conn, b).items()}
    assert got == {
        ("PL", "Mokotów"): "Warsaw", ("PL", "Bielany"): "Warsaw",
        ("GB", "Highbury"): "London",                   # not "City of London"
        ("UA", "Zhulyany"): "Kyiv",                     # "Kyiv City"
    }                                                   # Santiago: 40 km, a town
    # Both Bielanys in one library: only the Warsaw one rolls up.
    far = T.PlaceCount(d, "Poland", "Bielany", 52.34, 22.25, 50)
    rolled = T.roll_up_cities(b + [far], T.city_parents(conn, b + [far]))
    assert [x.city for x in rolled if x.country == "Poland"] == ["Warsaw", "Warsaw", "Warsaw", "Bielany"]


# --- sidecar hints ------------------------------------------------------------------


def test_sidecar_hints_use_unsuspect_neighbours(conn) -> None:
    from zoneinfo import ZoneInfo
    from immy import sidecar_check as sc
    conn.execute('ALTER TABLE asset ADD COLUMN "fileCreatedAt" timestamptz')
    conn.execute('ALTER TABLE asset_exif ADD COLUMN "timeZone" varchar')
    conn.execute('CREATE TABLE asset_file ("assetId" uuid, type varchar, path varchar)')
    t = datetime(2025, 10, 7, 15, 29, tzinfo=timezone.utc)

    def shot(minutes, lat, lon, tz, *, sidecar=False, owner=OWNER):
        aid = add(conn, datetime(2025, 10, 8, 3, 29), lat, lon, "Fiji", owner=owner)
        conn.execute('UPDATE asset SET "fileCreatedAt" = %s WHERE id = %s', (t + timedelta(minutes=minutes), aid))
        conn.execute('UPDATE asset_exif SET "timeZone" = %s WHERE "assetId" = %s', (tz, aid))
        if sidecar:
            conn.execute("INSERT INTO asset_file VALUES (%s, 'sidecar', 'x.xmp')", (aid,))
        return aid

    me = shot(0, 17.80, 177.42, "Pacific/Majuro", sidecar=True)       # the suspect
    shot(5, 17.80, 177.42, "Pacific/Majuro", sidecar=True)            # another suspect: ignored
    shot(-40, -17.75, 177.45, "Pacific/Fiji")                         # nearest clean shot
    shot(90, -17.70, 177.40, "Pacific/Fiji")
    shot(2, 40.0, -3.7, "Europe/Madrid", owner=OTHER)                 # someone else's: ignored
    got = sc.hints(conn, OWNER, {me: t})[me]
    assert (got.lat, got.lon) == (-17.75, 177.45)
    assert got.zone == ZoneInfo("Pacific/Fiji")



def test_sidecar_zone_vote_counts_utc_and_needs_a_clear_majority(conn) -> None:
    from immy import sidecar_check as sc
    conn.execute('ALTER TABLE asset ADD COLUMN "fileCreatedAt" timestamptz')
    conn.execute('ALTER TABLE asset_exif ADD COLUMN "timeZone" varchar')
    conn.execute('CREATE TABLE asset_file ("assetId" uuid, type varchar, path varchar)')
    t = datetime(2024, 1, 10, 12, 0, tzinfo=timezone.utc)

    def shot(minutes, tz, lat=38.7, lon=-9.1):
        aid = add(conn, datetime(2024, 1, 10, 12, 0), lat, lon, "Portugal")
        conn.execute('UPDATE asset SET "fileCreatedAt" = %s WHERE id = %s', (t + timedelta(minutes=minutes), aid))
        conn.execute('UPDATE asset_exif SET "timeZone" = %s WHERE "assetId" = %s', (tz, aid))
        return aid

    me = shot(0, "UTC", None, None)                      # the video: no GPS
    for m in range(5):
        shot(m + 1, "Europe/Lisbon")                     # Lisbon in winter: UTC+0, real
    shot(9, "Europe/Warsaw")
    shot(10, "UTC+2", None, None)                        # no GPS: doesn't vote
    # 5 Lisbon (+00:00 in January) of 7 votes: UTC is the local clock there,
    # so no zone is offered (and nothing changes).
    assert sc.hints(conn, OWNER, {me: t})[me].zone is None
    for m in range(12):
        shot(20 + m, "Europe/Warsaw")                    # 13 Warsaw vs 5 Lisbon (+1 UTC+2)
    assert str(sc.hints(conn, OWNER, {me: t})[me].zone) == "Europe/Warsaw"



def test_sidecar_zone_vote_skips_unverified_shots(conn) -> None:
    from immy import sidecar_check as sc
    conn.execute('ALTER TABLE asset ADD COLUMN "fileCreatedAt" timestamptz')
    conn.execute('ALTER TABLE asset_exif ADD COLUMN "timeZone" varchar')
    conn.execute('CREATE TABLE asset_file ("assetId" uuid, type varchar, path varchar)')
    t = datetime(2025, 10, 7, 15, 0, tzinfo=timezone.utc)

    def shot(minutes, tz, lat, lon):
        aid = add(conn, datetime(2025, 10, 8, 3, 0), lat, lon, "Fiji")
        conn.execute('UPDATE asset SET "fileCreatedAt" = %s WHERE id = %s', (t + timedelta(minutes=minutes), aid))
        conn.execute('UPDATE asset_exif SET "timeZone" = %s WHERE "assetId" = %s', (tz, aid))
        return aid

    me = shot(0, "UTC", None, None)
    # French Polynesia: mirrored ghosts read "Saipan" (+10), the truth is -10.
    ghosts = [shot(m + 1, "Pacific/Saipan", 17.5, 149.6) for m in range(3)]
    shot(30, "Pacific/Tahiti", -17.5, -149.6)
    assert str(sc.hints(conn, OWNER, {me: t})[me].zone) == "Pacific/Saipan"   # what the ghosts would do
    assert str(sc.hints(conn, OWNER, {me: t}, exclude=ghosts)[me].zone) == "Pacific/Tahiti"
