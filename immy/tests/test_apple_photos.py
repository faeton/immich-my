"""Tests for the Apple Photos reader.

We don't need a real `Photos.sqlite` — the reader only touches a handful
of columns on four tables. We build a minimal SQLite that matches the
column names our queries use and call the functions end-to-end.

If Apple renames or drops any of these columns in a future macOS, these
tests will start failing with a clear `no such column: Z…` error, which
is the point.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from immy import apple_photos, snapshot as snap


# --- fixtures -------------------------------------------------------------


_SCHEMA = """
CREATE TABLE ZPERSON (
    Z_PK INTEGER PRIMARY KEY,
    ZFULLNAME TEXT,
    ZDISPLAYNAME TEXT,
    ZFACECOUNT INTEGER,
    ZMERGETARGETPERSON INTEGER
);
CREATE TABLE ZASSET (
    Z_PK INTEGER PRIMARY KEY,
    ZUUID TEXT,
    ZFILENAME TEXT,
    ZTRASHEDSTATE INTEGER
);
CREATE TABLE ZADDITIONALASSETATTRIBUTES (
    Z_PK INTEGER PRIMARY KEY,
    ZASSET INTEGER,
    ZORIGINALFILENAME TEXT,
    ZORIGINALFILESIZE INTEGER
);
CREATE TABLE ZDETECTEDFACE (
    Z_PK INTEGER PRIMARY KEY,
    ZPERSONFORFACE INTEGER,
    ZASSETFORFACE INTEGER,
    ZCENTERX REAL,
    ZCENTERY REAL,
    ZSIZE REAL,
    ZSOURCEWIDTH INTEGER,
    ZSOURCEHEIGHT INTEGER,
    ZQUALITY REAL,
    ZMANUAL INTEGER
);
"""


def _add_person(conn, pk: int, name: str | None,
                face_count: int = 0, merge_to: int | None = None) -> None:
    conn.execute(
        "INSERT INTO ZPERSON (Z_PK, ZFULLNAME, ZDISPLAYNAME, ZFACECOUNT,"
        " ZMERGETARGETPERSON) VALUES (?, ?, ?, ?, ?)",
        (pk, name, name, face_count, merge_to),
    )


def _add_asset(conn, pk: int, uuid: str, filename: str, size: int | None,
               trashed: int = 0) -> None:
    conn.execute(
        "INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZTRASHEDSTATE)"
        " VALUES (?, ?, ?, ?)",
        (pk, uuid, uuid + ".heic", trashed),
    )
    conn.execute(
        "INSERT INTO ZADDITIONALASSETATTRIBUTES"
        " (Z_PK, ZASSET, ZORIGINALFILENAME, ZORIGINALFILESIZE)"
        " VALUES (?, ?, ?, ?)",
        (pk, pk, filename, size),
    )


def _add_face(conn, pk: int, person_pk: int | None, asset_pk: int,
              cx: float | None = 0.5, cy: float = 0.5, size: float = 0.1,
              sw: int = 1000, sh: int = 1000, quality: float = 0.5,
              manual: int = 0) -> None:
    conn.execute(
        "INSERT INTO ZDETECTEDFACE (Z_PK, ZPERSONFORFACE, ZASSETFORFACE,"
        " ZCENTERX, ZCENTERY, ZSIZE, ZSOURCEWIDTH, ZSOURCEHEIGHT,"
        " ZQUALITY, ZMANUAL) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pk, person_pk, asset_pk, cx, cy, size, sw, sh, quality, manual),
    )


@pytest.fixture
def photos_db(tmp_path: Path) -> Path:
    """Minimal Photos.sqlite seeded via schema + helpers below."""
    path = tmp_path / "Photos.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)

    # Ivan (named, 4 confirmed faces across 4 assets)
    _add_person(conn, pk=1, name="Ivan", face_count=4)
    for i, (uuid, fname, size) in enumerate([
        ("U1", "IMG_0001.HEIC", 1000),
        ("U2", "IMG_0002.HEIC", 2000),
        ("U3", "IMG_0003.HEIC", 3000),
        ("U4", "IMG_0004.HEIC", 4000),
    ], start=10):
        _add_asset(conn, pk=i, uuid=uuid, filename=fname, size=size)
        _add_face(conn, pk=100 + i, person_pk=1, asset_pk=i)

    # Mama (named, 2 faces only — under default min_faces=3)
    _add_person(conn, pk=2, name="Mama", face_count=2)
    for i, (uuid, fname, size) in enumerate([
        ("U5", "IMG_0005.HEIC", 5000),
        ("U6", "IMG_0006.HEIC", 6000),
    ], start=20):
        _add_asset(conn, pk=i, uuid=uuid, filename=fname, size=size)
        _add_face(conn, pk=100 + i, person_pk=2, asset_pk=i)

    # Person_3 (no name — Apple auto-cluster; should be filtered entirely)
    _add_person(conn, pk=3, name=None, face_count=10)
    _add_asset(conn, pk=30, uuid="U30", filename="IMG_0030.HEIC", size=30000)
    _add_face(conn, pk=130, person_pk=3, asset_pk=30)

    # Legacy merge chain: Person_4 ("IvanOld") merged → pk=1 ("Ivan").
    # Face rows still point at pk=4; after resolution they should roll up
    # into Ivan.
    _add_person(conn, pk=4, name="IvanOld", face_count=3, merge_to=1)
    for i, (uuid, fname, size) in enumerate([
        ("U7", "IMG_0007.HEIC", 7000),
        ("U8", "IMG_0008.HEIC", 8000),
        ("U9", "IMG_0009.HEIC", 9000),
    ], start=40):
        _add_asset(conn, pk=i, uuid=uuid, filename=fname, size=size)
        _add_face(conn, pk=100 + i, person_pk=4, asset_pk=i)

    # Edge: trashed asset with a face for Ivan — should be filtered.
    _add_asset(conn, pk=50, uuid="U50", filename="IMG_0050.HEIC",
               size=50000, trashed=1)
    _add_face(conn, pk=150, person_pk=1, asset_pk=50)

    # Edge: face with NULL bbox (Apple sometimes emits these in-progress).
    _add_asset(conn, pk=51, uuid="U51", filename="IMG_0051.HEIC", size=51000)
    _add_face(conn, pk=151, person_pk=1, asset_pk=51, cx=None)

    conn.commit()
    conn.close()
    return path


# --- resolve_db_path ------------------------------------------------------


def test_resolve_db_path_direct(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "Photos.sqlite"
    sqlite_path.touch()
    assert apple_photos.resolve_db_path(sqlite_path) == sqlite_path


def test_resolve_db_path_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "My.photoslibrary"
    (bundle / "database").mkdir(parents=True)
    sqlite_path = bundle / "database" / "Photos.sqlite"
    sqlite_path.touch()
    assert apple_photos.resolve_db_path(bundle) == sqlite_path


def test_resolve_db_path_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        apple_photos.resolve_db_path(tmp_path / "nope.photoslibrary")


# --- read_named_persons ---------------------------------------------------


def test_reads_named_skips_autoclusters(photos_db: Path) -> None:
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(conn, min_faces=1)
    finally:
        conn.close()
    names = {p.full_name for p in persons}
    assert "Ivan" in names
    assert "Mama" in names
    # Auto-cluster (NULL name) must not appear.
    assert None not in names
    # Merge-loser IvanOld must not appear either — its Z_PK was rolled up.
    assert "IvanOld" not in names


def test_min_faces_filters_small_persons(photos_db: Path) -> None:
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(conn, min_faces=3)
    finally:
        conn.close()
    names = [p.full_name for p in persons]
    assert "Mama" not in names, "Mama only has 2 faces, below min_faces=3"
    assert "Ivan" in names


def test_only_filter_restricts_to_named(photos_db: Path) -> None:
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(
            conn, min_faces=1, only={"Mama"},
        )
    finally:
        conn.close()
    assert [p.full_name for p in persons] == ["Mama"]


def test_merge_chain_rolls_faces_up(photos_db: Path) -> None:
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(conn, min_faces=1)
    finally:
        conn.close()
    ivan = next(p for p in persons if p.full_name == "Ivan")
    # 4 original Ivan faces + 3 from merged IvanOld = 7. The trashed
    # asset and the NULL-bbox row must not contribute.
    assert len(ivan.faces) == 7
    uuids = {f.apple_asset_uuid for f in ivan.faces}
    assert "U50" not in uuids  # trashed
    assert "U51" not in uuids  # NULL bbox


def test_face_carries_original_filename_and_size(photos_db: Path) -> None:
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(conn, min_faces=1)
    finally:
        conn.close()
    ivan = next(p for p in persons if p.full_name == "Ivan")
    by_name = {f.original_filename: f for f in ivan.faces}
    assert by_name["IMG_0001.HEIC"].original_size == 1000
    assert by_name["IMG_0007.HEIC"].original_size == 7000


# --- match_to_snapshot ----------------------------------------------------


def _make_snapshot(path: Path, rows: list[tuple[str, str, int]]) -> None:
    """Rows are `(asset_id, filename, size_bytes)`."""
    db = snap.create(path)
    try:
        db.executemany(
            "INSERT INTO assets (asset_id, filename, size_bytes, checksum,"
            " taken_at, asset_type, library_id) "
            "VALUES (?, ?, ?, NULL, NULL, 'IMAGE', 'lib1')",
            rows,
        )
        db.commit()
    finally:
        db.close()


def test_match_unique_wins(photos_db: Path, tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _make_snapshot(snap_path, [
        ("A-0001", "IMG_0001.HEIC", 1000),
        ("A-0002", "IMG_0002.HEIC", 2000),
    ])
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(conn, min_faces=1)
    finally:
        conn.close()
    sdb = snap.open_for_read(snap_path)
    try:
        matches = apple_photos.match_to_snapshot(persons, sdb)
    finally:
        sdb.close()
    ivan = next(p for p in persons if p.full_name == "Ivan")
    ivan_matches = matches[ivan.apple_pk]
    matched_ids = {m.immich_asset_id for m in ivan_matches}
    assert matched_ids == {"A-0001", "A-0002"}


def test_match_ambiguous_is_dropped(photos_db: Path, tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _make_snapshot(snap_path, [
        ("A-dupe1", "IMG_0001.HEIC", 1000),
        ("A-dupe2", "IMG_0001.HEIC", 1000),  # same name+size in two libraries
    ])
    conn = apple_photos.open_ro(photos_db)
    try:
        persons = apple_photos.read_named_persons(conn, min_faces=1)
    finally:
        conn.close()
    sdb = snap.open_for_read(snap_path)
    try:
        matches = apple_photos.match_to_snapshot(persons, sdb)
    finally:
        sdb.close()
    ivan = next(p for p in persons if p.full_name == "Ivan")
    matched_ids = {m.immich_asset_id for m in matches[ivan.apple_pk]}
    assert "A-dupe1" not in matched_ids
    assert "A-dupe2" not in matched_ids


def test_match_missing_size_skipped(tmp_path: Path) -> None:
    photos_path = tmp_path / "Photos.sqlite"
    conn = sqlite3.connect(photos_path)
    conn.executescript(_SCHEMA)
    _add_person(conn, pk=1, name="Ivan", face_count=1)
    _add_asset(conn, pk=10, uuid="U1", filename="IMG_X.HEIC", size=None)
    _add_face(conn, pk=100, person_pk=1, asset_pk=10)
    conn.commit()
    conn.close()

    snap_path = tmp_path / "snap.sqlite"
    _make_snapshot(snap_path, [("A-1", "IMG_X.HEIC", 0)])

    rconn = apple_photos.open_ro(photos_path)
    try:
        persons = apple_photos.read_named_persons(rconn, min_faces=1)
    finally:
        rconn.close()
    sdb = snap.open_for_read(snap_path)
    try:
        matches = apple_photos.match_to_snapshot(persons, sdb)
    finally:
        sdb.close()
    assert matches[persons[0].apple_pk] == []
