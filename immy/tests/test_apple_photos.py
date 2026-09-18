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
from datetime import datetime, timezone
from pathlib import Path

import pytest

from immy import apple_photos, snapshot as snap

_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _zdate(utc: datetime) -> float:
    """Convert a UTC datetime to Apple's ZDATECREATED (seconds since 2001)."""
    return (utc - _CORE_DATA_EPOCH).total_seconds()


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
    ZTRASHEDSTATE INTEGER,
    ZDATECREATED REAL
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
               trashed: int = 0, zdatecreated: float | None = None) -> None:
    conn.execute(
        "INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZTRASHEDSTATE, ZDATECREATED)"
        " VALUES (?, ?, ?, ?, ?)",
        (pk, uuid, uuid + ".heic", trashed, zdatecreated),
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


def _make_snapshot(path: Path, rows: list[tuple]) -> None:
    """Rows are `(asset_id, filename, size_bytes)`, optionally with a 4th
    `taken_at` (ISO string with UTC offset) element."""
    db = snap.create(path)
    try:
        normalized = [
            (r[0], r[1], r[2], r[3] if len(r) > 3 else None) for r in rows
        ]
        db.executemany(
            "INSERT INTO assets (asset_id, filename, size_bytes, checksum,"
            " taken_at, asset_type, library_id) "
            "VALUES (?, ?, ?, NULL, ?, 'IMAGE', 'lib1')",
            normalized,
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


def test_match_by_capture_time_survives_live_photo_rename(tmp_path: Path) -> None:
    """Immich renames the HEIC half of a Live Photo pair to dodge its
    video companion (`IMG_1434.HEIC` -> `IMG_1434(1).HEIC`). Filename+size
    can never find that; size+capture-instant should."""
    photos_path = tmp_path / "Photos.sqlite"
    conn = sqlite3.connect(photos_path)
    conn.executescript(_SCHEMA)
    _add_person(conn, pk=1, name="Ivan", face_count=1)
    captured = datetime(2019, 5, 1, 9, 20, 36, 987000, tzinfo=timezone.utc)
    _add_asset(conn, pk=10, uuid="U1", filename="IMG_1434.HEIC", size=2052106,
               zdatecreated=_zdate(captured))
    _add_face(conn, pk=100, person_pk=1, asset_pk=10)
    conn.commit()
    conn.close()

    snap_path = tmp_path / "snap.sqlite"
    _make_snapshot(snap_path, [
        # Correct asset: same size + same capture instant, different name.
        ("A-real", "IMG_1434(1).HEIC", 2052106, "2019-05-01T10:20:36.987000+01:00"),
        # Decoy: exact filename match, but wrong size/date (a recycled
        # IMG_#### counter from an unrelated year).
        ("A-decoy", "IMG_1434.HEIC", 999999, "2016-01-01T00:00:00+00:00"),
    ])
    conn = apple_photos.open_ro(photos_path)
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
    assert matched_ids == {"A-real"}


def test_match_capture_time_ambiguous_dropped(tmp_path: Path) -> None:
    """Two candidates with the same size within the tolerance window, and
    neither filename matching — must drop rather than guess."""
    photos_path = tmp_path / "Photos.sqlite"
    conn = sqlite3.connect(photos_path)
    conn.executescript(_SCHEMA)
    _add_person(conn, pk=1, name="Ivan", face_count=1)
    captured = datetime(2020, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    _add_asset(conn, pk=10, uuid="U1", filename="IMG_2000.HEIC", size=500000,
               zdatecreated=_zdate(captured))
    _add_face(conn, pk=100, person_pk=1, asset_pk=10)
    conn.commit()
    conn.close()

    snap_path = tmp_path / "snap.sqlite"
    _make_snapshot(snap_path, [
        ("A-1", "IMG_9001.HEIC", 500000, "2020-03-01T12:00:01+00:00"),
        ("A-2", "IMG_9002.HEIC", 500000, "2020-03-01T12:00:02+00:00"),
    ])
    conn = apple_photos.open_ro(photos_path)
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
    assert matches[ivan.apple_pk] == []


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


# --- apple_bbox_norm --------------------------------------------------------


def _face(uuid="U", cx=0.5, cy=0.5, fsize=0.1) -> apple_photos.AppleFace:
    return apple_photos.AppleFace(
        apple_asset_uuid=uuid, original_filename="IMG.HEIC", original_size=1000,
        capture_utc=None, center_x=cx, center_y=cy, size=fsize,
        source_width=1000, source_height=1000, quality=None, manual=False,
    )


def test_apple_bbox_norm_flips_y() -> None:
    # cy=0.8 is bottom-left-origin (near the top of frame in real terms) —
    # must land near y=0 (top) once flipped to top-left-origin.
    box = apple_photos.apple_bbox_norm(_face(cx=0.3, cy=0.8, fsize=0.2))
    assert box == pytest.approx((0.2, 0.1, 0.4, 0.3))


def test_apple_bbox_norm_none_for_zero_size() -> None:
    assert apple_photos.apple_bbox_norm(_face(fsize=0.0)) is None


# --- build_person_plans -----------------------------------------------------


def _existing(face_id, person_id=None, name=None, cx=0.5, cy=0.5, s=0.1):
    half = s / 2
    return apple_photos.ExistingFace(
        face_id=face_id, person_id=person_id, person_name=name,
        x1=cx - half, y1=cy - half, x2=cx + half, y2=cy + half,
    )


def _person(pk, name, faces):
    return apple_photos.ApplePerson(apple_pk=pk, full_name=name, display_name=None, faces=faces)


def test_build_person_plans_dominant_cluster_wins() -> None:
    faces = [_face(f"U{i}") for i in range(4)]
    person = _person(1, "Ivan", faces)
    matches = {1: [apple_photos.FaceMatch(f, f"A{i}") for i, f in enumerate(faces)]}
    # 3 faces land on the same unnamed existing cluster P1, 1 on a rival P2.
    existing = {
        "A0": [_existing("F0", "P1", "")],
        "A1": [_existing("F1", "P1", "")],
        "A2": [_existing("F2", "P1", "")],
        "A3": [_existing("F3", "P2", "")],
    }
    plans = apple_photos.build_person_plans([person], matches, existing)
    plan = plans[0]
    assert plan.target_person_id == "P1"
    assert plan.target_votes == 3
    assert plan.total_votes == 4


def test_build_person_plans_below_min_votes_no_target() -> None:
    faces = [_face(f"U{i}") for i in range(2)]
    person = _person(1, "Ivan", faces)
    matches = {1: [apple_photos.FaceMatch(f, f"A{i}") for i, f in enumerate(faces)]}
    # Only 2 votes for P1 — below MIN_VOTES=3 even at 100% confidence.
    existing = {
        "A0": [_existing("F0", "P1", "")],
        "A1": [_existing("F1", "P1", "")],
    }
    plans = apple_photos.build_person_plans([person], matches, existing)
    assert plans[0].target_person_id is None


def test_build_person_plans_orphans_only_attached_with_target() -> None:
    faces = [_face(f"U{i}") for i in range(4)]
    person = _person(1, "Ivan", faces)
    matches = {1: [apple_photos.FaceMatch(f, f"A{i}") for i, f in enumerate(faces)]}
    existing = {
        "A0": [_existing("F0", "P1", "")],
        "A1": [_existing("F1", "P1", "")],
        "A2": [_existing("F2", "P1", "")],
        "A3": [_existing("F3", None, None)],  # unclustered — orphan
    }
    plans = apple_photos.build_person_plans([person], matches, existing)
    plan = plans[0]
    assert plan.target_person_id == "P1"
    assert plan.orphan_face_ids == ["F3"]


def test_build_person_plans_conflict_and_already_named() -> None:
    faces = [_face(f"U{i}") for i in range(2)]
    person = _person(1, "Ivan", faces)
    matches = {1: [apple_photos.FaceMatch(f, f"A{i}") for i, f in enumerate(faces)]}
    existing = {
        "A0": [_existing("F0", "P-other", "Someone Else")],  # conflict
        "A1": [_existing("F1", "P-ivan", "Ivan")],  # already correctly named
    }
    plans = apple_photos.build_person_plans([person], matches, existing)
    plan = plans[0]
    assert plan.target_person_id is None  # neither counts as a vote
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0][1:] == ("P-other", "Someone Else")
    assert plan.already_named == 1


def test_build_person_plans_no_detection() -> None:
    faces = [_face("U0", fsize=0.0), _face("U1")]  # first has no usable bbox
    person = _person(1, "Ivan", faces)
    matches = {1: [
        apple_photos.FaceMatch(faces[0], "A0"),
        apple_photos.FaceMatch(faces[1], "A1"),  # A1 has no existing_faces entry at all
    ]}
    plans = apple_photos.build_person_plans([person], matches, {})
    assert plans[0].no_detection == 2
