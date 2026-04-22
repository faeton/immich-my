"""Portable snapshot of the Immich library for external-disk matching.

Dumps `(asset_id, filename, size, checksum, taken_at, asset_type,
library_id)` for every asset into a standalone SQLite file. The snapshot
then travels — to another laptop, an external drive, a backup machine —
where `immy find-duplicates` can answer "is this file already in Immich?"
without any network access.

Schema is intentionally minimal. Anything we can recompute locally from the
file itself (like mtime) is not cached. Anything Immich-side that would
require a live DB connection (like `description` or CLIP embeddings) is
also out of scope for this tool — see `PLAN.md` for the extended variant
(`find-similar`) that adds embeddings.

Checksums are stored as raw 20-byte BLOBs to save ~40% vs the base64 text
form Immich keeps in `assets.checksum`. Decoded once here, re-encoded only
if something external needs the text form.
"""

from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  asset_id   TEXT PRIMARY KEY,
  filename   TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  checksum   BLOB,
  taken_at   TEXT,
  asset_type TEXT NOT NULL,
  library_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_filename_size ON assets (filename, size_bytes);
CREATE INDEX IF NOT EXISTS idx_checksum      ON assets (checksum);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Pulls every asset's identity + size. LEFT JOIN on asset_exif because some
# assets (rare: freshly inserted, pre-exif-extract) may have no exif row
# yet. Those still get emitted with NULL size/taken_at rather than silently
# dropped. `asset_exif.fileSizeInByte` is populated by immy/process.py and
# by Immich's library scan.
_SELECT_ASSETS = """
SELECT
  a.id,
  a."originalFileName",
  e."fileSizeInByte",
  a.checksum,
  e."dateTimeOriginal",
  a.type,
  a."libraryId"
FROM asset a
LEFT JOIN asset_exif e ON e."assetId" = a.id
WHERE a."deletedAt" IS NULL
"""

_SELECT_ASSETS_BY_LIBRARY = _SELECT_ASSETS + ' AND a."libraryId" = %s'


@dataclass(frozen=True)
class AssetRow:
    """One row in the snapshot. `size_bytes` / `checksum` / `taken_at` may
    be None if Immich hasn't finished extracting exif yet."""

    asset_id: str
    filename: str
    size_bytes: int | None
    checksum: bytes | None
    taken_at: str | None
    asset_type: str
    library_id: str | None


def decode_immich_checksum(raw) -> bytes | None:
    """Immich's `asset.checksum` is `bytea` — psycopg returns `memoryview`
    or `bytes`. Normalise to raw `bytes`. If the column ever comes back as
    text (old schema variants) we fall back to base64 decode."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, memoryview):
        return bytes(raw)
    if isinstance(raw, str):
        return base64.b64decode(raw)
    raise TypeError(f"unexpected checksum type {type(raw)!r}")


def _isoformat(value) -> str | None:
    """Postgres `timestamp`/`timestamptz` comes back as `datetime`. We
    persist ISO8601 text in SQLite — it's portable and SQLite's date
    functions understand it."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def fetch_rows(conn, library_id: str | None = None) -> Iterable[AssetRow]:
    """Stream asset rows from a live Immich Postgres connection.

    `library_id=None` dumps everything the Immich user can see (every
    library they own). Pass a specific UUID to restrict.

    Uses a server-side cursor — on libraries past ~100k assets, the
    default client-side buffering is wasteful.
    """
    sql = _SELECT_ASSETS_BY_LIBRARY if library_id else _SELECT_ASSETS
    params = (library_id,) if library_id else ()
    with conn.cursor(name="immy_snapshot_cursor") as cur:
        cur.itersize = 5000
        cur.execute(sql, params)
        for row in cur:
            asset_id, filename, size, checksum, taken_at, asset_type, lib = row
            yield AssetRow(
                asset_id=str(asset_id),
                filename=str(filename) if filename is not None else "",
                size_bytes=int(size) if size is not None else None,
                checksum=decode_immich_checksum(checksum),
                taken_at=_isoformat(taken_at),
                asset_type=str(asset_type),
                library_id=str(lib) if lib is not None else None,
            )


def create(path: Path) -> sqlite3.Connection:
    """Create a fresh snapshot file, dropping any existing one at `path`.

    Returns an open connection with the schema already applied. Caller is
    responsible for closing it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript(_CREATE_SCHEMA)
    db.commit()
    return db


def write_rows(db: sqlite3.Connection, rows: Iterable[AssetRow]) -> int:
    """Insert `rows` into the snapshot. Returns the count written."""
    count = 0
    # executemany + batched commit keeps memory low on 100k+ libraries
    # without the write-amplification of row-by-row commits.
    BATCH = 2000
    batch: list[tuple] = []
    cur = db.cursor()
    for r in rows:
        batch.append((
            r.asset_id, r.filename, r.size_bytes, r.checksum,
            r.taken_at, r.asset_type, r.library_id,
        ))
        if len(batch) >= BATCH:
            cur.executemany(
                "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)", batch,
            )
            db.commit()
            count += len(batch)
            batch.clear()
    if batch:
        cur.executemany(
            "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)", batch,
        )
        db.commit()
        count += len(batch)
    return count


def write_meta(db: sqlite3.Connection, *, server_host: str,
               library_id: str | None, asset_count: int) -> None:
    """Stash metadata about *this* snapshot into the `meta` table.

    Intentionally text-only so the file is grep-able and dumpable with
    `sqlite3 snapshot.sqlite .dump` on any machine without schema knowledge.
    """
    rows = [
        ("schema_version", str(SCHEMA_VERSION)),
        ("created_at", datetime.now(timezone.utc).isoformat()),
        ("server_host", server_host),
        ("library_id", library_id or ""),
        ("asset_count", str(asset_count)),
    ]
    db.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", rows,
    )
    db.commit()


# --- read side (used by find-duplicates) ----------------------------------


@dataclass(frozen=True)
class SnapshotMatch:
    """One row returned when a local file matches something in the snapshot."""

    asset_id: str
    filename: str
    size_bytes: int
    checksum: bytes | None


def open_for_read(path: Path) -> sqlite3.Connection:
    """Open a snapshot read-only. Raises FileNotFoundError if missing."""
    if not path.exists():
        raise FileNotFoundError(f"snapshot not found: {path}")
    # `mode=ro` via URI prevents accidental writes; SQLite otherwise opens
    # read-write by default.
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def match_name_size(db: sqlite3.Connection, filename: str,
                    size_bytes: int) -> list[SnapshotMatch]:
    """Return every asset whose `(filename, size)` matches exactly.

    Usually 0 or 1 rows. Rare 2+ case: same filename + size present in two
    libraries (e.g. duplicate imports). Caller can decide how to report.
    """
    cur = db.execute(
        "SELECT asset_id, filename, size_bytes, checksum"
        " FROM assets WHERE filename = ? AND size_bytes = ?",
        (filename, size_bytes),
    )
    return [
        SnapshotMatch(
            asset_id=r[0], filename=r[1], size_bytes=r[2], checksum=r[3],
        )
        for r in cur.fetchall()
    ]


def match_checksum(db: sqlite3.Connection, checksum: bytes) -> list[SnapshotMatch]:
    """Return every asset whose SHA1 checksum matches. Catches renames."""
    cur = db.execute(
        "SELECT asset_id, filename, size_bytes, checksum"
        " FROM assets WHERE checksum = ?",
        (checksum,),
    )
    return [
        SnapshotMatch(
            asset_id=r[0], filename=r[1], size_bytes=r[2], checksum=r[3],
        )
        for r in cur.fetchall()
    ]


def read_meta(db: sqlite3.Connection) -> dict[str, str]:
    """Return the full meta table as a dict."""
    return {k: v for k, v in db.execute("SELECT key, value FROM meta")}
