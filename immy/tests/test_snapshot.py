"""Tests for the portable Immich snapshot.

We don't hit a real Postgres — `fetch_rows` takes a cursor-shaped object,
so a fake connection is enough to cover the write-side glue. The read-side
(`match_name_size`, `match_checksum`, `open_for_read`) is exercised
end-to-end with a real on-disk SQLite file.
"""

from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from immy import snapshot as snap


# --- fakes ----------------------------------------------------------------


class _FakeCursor:
    """Stand-in for a psycopg server-side cursor. Only the bits `fetch_rows`
    touches — context manager, `execute`, `itersize`, iteration."""

    def __init__(self, rows):
        self._rows = rows
        self.itersize = None
        self.last_sql: str | None = None
        self.last_params: tuple = ()

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()):
        self.last_sql = sql
        self.last_params = params
    def __iter__(self): return iter(self._rows)


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.last_cursor: _FakeCursor | None = None
    def cursor(self, name=None):
        self.last_cursor = _FakeCursor(self.rows)
        return self.last_cursor


# --- decode_immich_checksum ----------------------------------------------


def test_decode_bytes_passthrough() -> None:
    assert snap.decode_immich_checksum(b"\x00" * 20) == b"\x00" * 20


def test_decode_memoryview() -> None:
    mv = memoryview(b"\x01\x02\x03")
    assert snap.decode_immich_checksum(mv) == b"\x01\x02\x03"


def test_decode_base64_fallback() -> None:
    raw = b"\xde\xad\xbe\xef" * 5
    b64 = base64.b64encode(raw).decode()
    assert snap.decode_immich_checksum(b64) == raw


def test_decode_none_is_none() -> None:
    assert snap.decode_immich_checksum(None) is None


# --- fetch_rows round-trip -----------------------------------------------


def test_fetch_rows_handles_all_columns() -> None:
    dt = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn = _FakeConn([
        ("uuid-1", "DSC_0001.JPG", 4_123_456, b"\xaa" * 20,
         dt, "IMAGE", "lib-1"),
        ("uuid-2", "clip.mp4", None, None,
         None, "VIDEO", None),
    ])
    rows = list(snap.fetch_rows(conn))
    assert len(rows) == 2
    assert rows[0].asset_id == "uuid-1"
    assert rows[0].size_bytes == 4_123_456
    assert rows[0].checksum == b"\xaa" * 20
    assert rows[0].taken_at == dt.isoformat()
    assert rows[0].asset_type == "IMAGE"
    assert rows[0].library_id == "lib-1"
    # NULLs survive as None, not crash.
    assert rows[1].size_bytes is None
    assert rows[1].checksum is None
    assert rows[1].taken_at is None
    assert rows[1].library_id is None


def test_fetch_rows_library_filter_passes_param() -> None:
    conn = _FakeConn([])
    list(snap.fetch_rows(conn, library_id="lib-xyz"))
    assert conn.last_cursor.last_params == ("lib-xyz",)
    assert '"libraryId" = %s' in conn.last_cursor.last_sql


def test_fetch_rows_without_library_no_param() -> None:
    conn = _FakeConn([])
    list(snap.fetch_rows(conn))
    assert conn.last_cursor.last_params == ()


# --- write + read round-trip ---------------------------------------------


def _make_row(asset_id: str, name: str, size: int,
              checksum: bytes | None = None) -> snap.AssetRow:
    return snap.AssetRow(
        asset_id=asset_id, filename=name, size_bytes=size,
        checksum=checksum, taken_at=None, asset_type="IMAGE",
        library_id=None,
    )


def test_create_writes_schema(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert tables == {"assets", "meta"}
    finally:
        db.close()


def test_create_overwrites_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    p.write_bytes(b"garbage")
    db = snap.create(p)
    db.close()
    # New file is a valid SQLite DB (open succeeds).
    sqlite3.connect(p).close()


def test_write_rows_returns_count_and_persists(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    count = snap.write_rows(db, [
        _make_row("a", "x.jpg", 100, b"\x01" * 20),
        _make_row("b", "y.jpg", 200, b"\x02" * 20),
        _make_row("c", "z.jpg", 300),  # no checksum
    ])
    db.close()
    assert count == 3
    # Re-open to verify persistence.
    rd = sqlite3.connect(p)
    rows = list(rd.execute("SELECT asset_id, filename, size_bytes FROM assets"))
    assert sorted(rows) == [("a", "x.jpg", 100), ("b", "y.jpg", 200), ("c", "z.jpg", 300)]


def test_write_rows_batches_past_the_flush_boundary(tmp_path: Path) -> None:
    # Batch size internal is 2000; make sure both the batched flush and the
    # trailing-remainder flush fire.
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    rows = [_make_row(f"a{i}", f"n{i}.jpg", i) for i in range(2500)]
    count = snap.write_rows(db, rows)
    db.close()
    assert count == 2500
    rd = sqlite3.connect(p)
    (total,) = rd.execute("SELECT count(*) FROM assets").fetchone()
    assert total == 2500


def test_write_meta_stores_expected_keys(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    snap.write_meta(db, server_host="pg.example:5432",
                    library_id="lib-1", asset_count=42)
    db.close()
    db = snap.open_for_read(p)
    meta = snap.read_meta(db)
    db.close()
    assert meta["server_host"] == "pg.example:5432"
    assert meta["library_id"] == "lib-1"
    assert meta["asset_count"] == "42"
    assert meta["schema_version"] == str(snap.SCHEMA_VERSION)
    assert "created_at" in meta


def test_match_name_size_returns_snapshot_match(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    snap.write_rows(db, [
        _make_row("a", "photo.jpg", 500, b"\xaa" * 20),
        _make_row("b", "other.jpg", 500),
    ])
    db.close()
    rd = snap.open_for_read(p)
    try:
        matches = snap.match_name_size(rd, "photo.jpg", 500)
        assert len(matches) == 1
        assert matches[0].asset_id == "a"
        assert matches[0].checksum == b"\xaa" * 20
    finally:
        rd.close()


def test_match_name_size_empty_on_miss(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    snap.write_rows(db, [_make_row("a", "photo.jpg", 500)])
    db.close()
    rd = snap.open_for_read(p)
    try:
        assert snap.match_name_size(rd, "photo.jpg", 501) == []
        assert snap.match_name_size(rd, "nope.jpg", 500) == []
    finally:
        rd.close()


def test_match_checksum(tmp_path: Path) -> None:
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    snap.write_rows(db, [
        _make_row("a", "photo.jpg", 500, b"\xaa" * 20),
        _make_row("b", "renamed.jpg", 500, b"\xaa" * 20),
        _make_row("c", "other.jpg", 500, b"\xbb" * 20),
    ])
    db.close()
    rd = snap.open_for_read(p)
    try:
        matches = snap.match_checksum(rd, b"\xaa" * 20)
        assert {m.asset_id for m in matches} == {"a", "b"}
    finally:
        rd.close()


def test_open_for_read_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        snap.open_for_read(tmp_path / "nope.sqlite")


def test_empty_library_produces_valid_snapshot(tmp_path: Path) -> None:
    # Regression: writing 0 rows must still leave a valid DB with schema +
    # meta, not crash the batcher on an empty iterable.
    p = tmp_path / "snap.sqlite"
    db = snap.create(p)
    count = snap.write_rows(db, iter(()))
    snap.write_meta(db, server_host="pg", library_id=None, asset_count=count)
    db.close()
    assert count == 0
    rd = snap.open_for_read(p)
    (n,) = rd.execute("SELECT count(*) FROM assets").fetchone()
    assert n == 0
    rd.close()
