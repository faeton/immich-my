"""Postgres connection helper for Phase Y direct-to-DB inserts.

`immy process` writes `asset` + `asset_exif` rows into the Immich database
directly so the library scan becomes a no-op. See docs/IMMICH-INGEST.md §1.

Keep this module small — it owns connection bootstrap and a single library
lookup. Row-building lives in `process.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .config import PgConfig


@dataclass(frozen=True)
class LibraryInfo:
    """Cached row from `library` needed to write external-library assets.

    - `id` — the library UUID we write into `asset.libraryId`.
    - `owner_id` — the user UUID we write into `asset.ownerId`; for an
      external library this is fixed to whoever created the library.
    - `container_root` — the import-path prefix as Immich (inside the
      container) sees it. Our `originalPath` values must be anchored
      under this.
    """

    id: str
    owner_id: str
    container_root: str


def connect(cfg: PgConfig) -> psycopg.Connection:
    """Open a new autocommit-off connection. Caller owns it and must close."""
    return psycopg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        dbname=cfg.database,
    )


def fetch_library_info(conn: psycopg.Connection, library_id: str) -> LibraryInfo:
    """Read `ownerId` and first `importPaths[0]` for the configured library.

    Raises LookupError if the library row is missing or has no import paths —
    either condition means `immy process` cannot produce a valid originalPath.
    """
    row = conn.execute(
        'SELECT "ownerId", "importPaths" FROM library WHERE id = %s',
        (library_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"library {library_id} not found in Immich DB")
    owner_id, import_paths = row
    if not import_paths:
        raise LookupError(
            f"library {library_id} has no importPaths — set one in "
            "Immich → Admin → Libraries → External before running process"
        )
    return LibraryInfo(
        id=library_id,
        owner_id=str(owner_id),
        container_root=str(import_paths[0]).rstrip("/"),
    )
