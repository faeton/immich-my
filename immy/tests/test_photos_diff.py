"""Tests for `immy photos diff` — Photos.sqlite vs an Immich snapshot.

Minimal Photos.sqlite with only the columns `photos_diff` reads, plus a real
snapshot built through `snapshot.create`/`write_rows`, so a column rename on
either side fails here loudly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from immy import photos_diff as pd, snapshot as snap

_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

_SCHEMA = """
CREATE TABLE ZASSET (
    Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZTRASHEDSTATE INTEGER,
    ZDATECREATED REAL, ZADDEDDATE REAL, ZKIND INTEGER, ZVISIBILITYSTATE INTEGER
);
CREATE TABLE ZADDITIONALASSETATTRIBUTES (
    Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER,
    ZORIGINALFILENAME TEXT, ZORIGINALFILESIZE INTEGER
);
CREATE TABLE ZEXTENDEDATTRIBUTES (
    Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER, ZCAMERAMODEL TEXT
);
"""

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _core(t: datetime) -> float:
    return (t - _EPOCH).total_seconds()


def _photos(rows: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    for pk, r in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO ZASSET VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pk, r["uuid"], r.get("trashed", 0), _core(r.get("created", T0)),
             _core(r.get("added", T0)), r.get("kind", 0), r.get("vis", 0)),
        )
        conn.execute(
            "INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?, ?, ?, ?)",
            (pk, pk, r["name"], r.get("size")),
        )
        conn.execute(
            "INSERT INTO ZEXTENDEDATTRIBUTES VALUES (?, ?, ?)",
            (pk, pk, r.get("camera")),
        )
    return conn


def _snapshot(tmp_path: Path, rows: list[tuple[str, int | None, datetime | None]]) -> sqlite3.Connection:
    db = snap.create(tmp_path / "snap.sqlite")
    snap.write_rows(db, [
        snap.AssetRow(
            asset_id=f"a{i}", filename=name, size_bytes=size, checksum=None,
            taken_at=taken.isoformat() if taken else None,
            asset_type="IMAGE", library_id=None,
        )
        for i, (name, size, taken) in enumerate(rows)
    ])
    return db


def _status(result: pd.DiffResult) -> dict[str, str]:
    return {c.asset.uuid: c.status for c in result.items}


def test_exact_match_is_case_insensitive_name_plus_size(tmp_path) -> None:
    photos = _photos([{"uuid": "U1", "name": "IMG_0001.HEIC", "size": 100,
                       "created": T0 + timedelta(days=3)}])
    immich = _snapshot(tmp_path, [("img_0001.heic", 100, None)])
    assert _status(pd.diff(photos, immich, None)) == {"U1": "exact"}


def test_same_name_different_size_and_time_is_missing(tmp_path) -> None:
    # IMG_NNNN counters wrap: an older photo with the same name must not
    # hide a new one. This was the 3.7k false-positive on the real library.
    photos = _photos([{"uuid": "U1", "name": "IMG_8976.HEIC", "size": 2_538_532}])
    immich = _snapshot(tmp_path, [("IMG_8976.HEIC", 1_366_104, T0 - timedelta(days=10))])
    assert _status(pd.diff(photos, immich, None)) == {"U1": "missing"}


def test_capture_time_within_tolerance_matches_renamed_copy(tmp_path) -> None:
    photos = _photos([
        {"uuid": "near", "name": "IMG_1.HEIC", "size": 5, "created": T0},
        {"uuid": "far", "name": "IMG_2.HEIC", "size": 5, "created": T0 + timedelta(seconds=30)},
    ])
    immich = _snapshot(tmp_path, [
        ("telegram-123.jpg", 1, T0 + timedelta(milliseconds=800)),
    ])
    assert _status(pd.diff(photos, immich, None)) == {"near": "time", "far": "missing"}


def test_offset_aware_taken_at_compares_in_utc(tmp_path) -> None:
    lisbon = timezone(timedelta(hours=1))
    photos = _photos([{"uuid": "U1", "name": "IMG_1.HEIC", "size": 5, "created": T0}])
    immich = _snapshot(tmp_path, [("x.jpg", 1, T0.astimezone(lisbon))])
    assert _status(pd.diff(photos, immich, None)) == {"U1": "time"}


def test_added_since_filters_on_date_added_not_capture(tmp_path) -> None:
    photos = _photos([
        # Old capture, added recently (e.g. AirDropped): in scope.
        {"uuid": "old-new", "name": "a.jpg", "size": 1,
         "created": T0 - timedelta(days=900), "added": T0},
        # Added before the cut: out of scope.
        {"uuid": "early", "name": "b.jpg", "size": 1, "added": T0 - timedelta(days=30)},
    ])
    immich = _snapshot(tmp_path, [])
    result = pd.diff(photos, immich, T0 - timedelta(days=1))
    assert _status(result) == {"old-new": "missing"}


def test_trashed_and_hidden_burst_frames_are_skipped(tmp_path) -> None:
    photos = _photos([
        {"uuid": "keep", "name": "a.jpg", "size": 1},
        {"uuid": "trash", "name": "b.jpg", "size": 1, "trashed": 1},
        {"uuid": "burst", "name": "c.jpg", "size": 1, "vis": 2},
    ])
    result = pd.diff(photos, _snapshot(tmp_path, []), None)
    assert _status(result) == {"keep": "missing"}
    assert result.skipped_hidden_bursts == 1


def test_null_sizes_never_exact_match(tmp_path) -> None:
    photos = _photos([{"uuid": "U1", "name": "a.mov", "size": None, "kind": 1,
                       "created": T0 + timedelta(days=5)}])
    immich = _snapshot(tmp_path, [("a.mov", None, None)])
    result = pd.diff(photos, immich, None)
    assert _status(result) == {"U1": "missing"}
    assert result.items[0].asset.kind == "video"
