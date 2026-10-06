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
from datetime import date, datetime, timezone

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
