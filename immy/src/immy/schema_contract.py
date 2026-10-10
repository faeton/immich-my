"""The Immich Postgres columns immy writes directly, and the schema they
were verified against.

`immy process` / `promote` / `sync-offline` bypass Immich's API and write
rows into its database. An Immich upgrade that drops, renames, retypes or
newly requires a column turns those writes into mid-run errors (or worse,
silently wrong data — 3.x changed `asset.duration` from a varchar to
integer milliseconds). Three things keep that honest:

- `data/immich_schema.json` — a snapshot of every table below as the
  pinned Immich release ships it (regenerate with
  `scripts/regen_immich_schema.py`).
- `tests/test_schema_contract.py` — extracts every INSERT/UPDATE from the
  source and checks it against `WRITE_COLUMNS` and the snapshot, including
  that each INSERT supplies every NOT NULL column without a default.
- `live_schema_problems` — the same contract checked against the live DB.
  `doctor` reports it; `process` / `promote` / `sync-offline` abort on it
  before writing anything.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).parent / "data" / "immich_schema.json"

# Every column immy writes (INSERT column lists, UPDATE SET targets, ON
# CONFLICT DO UPDATE targets), per table. The contract test asserts this is
# exactly what the source SQL writes — add a column to a statement and the
# test tells you to add it here, which in turn puts it under the live guard.
WRITE_COLUMNS: dict[str, tuple[str, ...]] = {
    "asset": (
        "id", "ownerId", "type", "originalPath", "originalFileName",
        "checksum", "checksumAlgorithm", "fileCreatedAt", "fileModifiedAt",
        "localDateTime", "duration", "libraryId", "isExternal",
        "width", "height", "isOffline", "deletedAt", "status",
    ),
    "asset_exif": (
        "assetId", "description", "make", "model", "lensModel", "orientation",
        "exifImageWidth", "exifImageHeight", "fileSizeInByte",
        "dateTimeOriginal", "modifyDate", "fNumber", "focalLength", "iso",
        "exposureTime", "fps", "latitude", "longitude", "timeZone",
        "lockedProperties", "country", "state", "city", "tags",
    ),
    "asset_file": (
        "assetId", "type", "path", "isEdited", "isProgressive", "isTransparent",
    ),
    "asset_face": (
        "id", "assetId", "personGroupId", "imageWidth", "imageHeight",
        "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
        "sourceType", "isVisible",
    ),
    "face_search": ("faceId", "embedding"),
    "tag_asset": ("assetId", "tagId"),
    "smart_search": ("assetId", "embedding"),
    "person": ("name",),
}

# Columns immy reads, joins or filters on but never writes: a rename here
# (3.3 turned `person.id`/`asset_face.personId` into `personGroupId`) breaks
# a query without touching any write. Checked for presence only.
READ_COLUMNS: dict[str, tuple[str, ...]] = {
    "asset": ("id", "ownerId", "deletedAt", "localDateTime", "fileCreatedAt",
              "originalPath", "originalFileName", "type", "visibility", "stackId",
              "libraryId", "checksum"),
    "asset_exif": ("assetId", "country", "city", "latitude", "longitude", "timeZone",
                   "tags", "lockedProperties", "dateTimeOriginal"),
    "asset_file": ("assetId", "type", "path", "isEdited"),
    "asset_face": ("id", "assetId", "personGroupId", "deletedAt", "sourceType"),
    "face_search": ("faceId", "embedding"),
    "person": ("ownerId", "personGroupId", "name", "faceAssetId"),
    "tag": ("id", "userId", "value"),
    "tag_asset": ("assetId", "tagId"),
    "stack": ("id", "primaryAssetId"),
    "album": ("id", "albumName", "description", "deletedAt"),
    "album_asset": ("albumId", "assetId"),
    "user": ("id", "email", "deletedAt"),
    "geodata_places": ("id", "name", "admin1Name", "admin2Name", "alternateNames",
                       "countryCode", "latitude", "longitude"),
    "naturalearth_countries": ("admin_a3", "coordinates"),
}

# Tables immy INSERTs into. A NOT NULL column without a default that appears
# here in a newer Immich would make those INSERTs fail, so the live guard
# rejects one the snapshot didn't already require (the contract test
# guarantees every snapshot-required column is supplied).
INSERT_TABLES: frozenset[str] = frozenset({
    "asset", "asset_exif", "asset_file", "asset_face", "face_search", "smart_search",
    "tag_asset",
})


class SchemaMismatch(RuntimeError):
    """The live Immich schema no longer matches what immy writes."""


@lru_cache(maxsize=1)
def load_snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text())


def required_columns(columns: dict[str, dict]) -> set[str]:
    """NOT NULL columns with no default — an INSERT must supply each one."""
    return {
        name for name, col in columns.items()
        if not col["is_nullable"] and col["column_default"] is None
    }


_LIVE_COLUMNS_SQL = """
SELECT column_name, udt_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = %s
"""


def fetch_live_columns(conn, table: str) -> dict[str, dict]:
    rows = conn.execute(_LIVE_COLUMNS_SQL, (table,)).fetchall()
    return {
        name: {
            "udt_name": udt,
            "is_nullable": nullable == "YES",
            "column_default": default,
        }
        for name, udt, nullable, default in rows
    }


def live_schema_problems(conn) -> dict[str, list[str]]:
    """Check the live DB against the contract. Returns `table → problems`
    for every table in `WRITE_COLUMNS` and `READ_COLUMNS` (an empty list
    means the table is fine). Read-only: information_schema queries only."""
    snapshot = load_snapshot()["tables"]
    out: dict[str, list[str]] = {}
    for table, written in WRITE_COLUMNS.items():
        live = fetch_live_columns(conn, table)
        if not live:
            out[table] = ["table missing"]
            continue
        problems: list[str] = []
        missing = [c for c in written if c not in live]
        if missing:
            problems.append(f"missing columns: {', '.join(missing)}")
        expected = snapshot[table]
        retyped = [
            f"{c} is {live[c]['udt_name']} (expected {expected[c]['udt_name']})"
            for c in written
            if c in live and live[c]["udt_name"] != expected[c]["udt_name"]
        ]
        if retyped:
            problems.append(f"changed type: {', '.join(retyped)}")
        if table in INSERT_TABLES:
            new_required = sorted(required_columns(live) - required_columns(expected))
            if new_required:
                problems.append(
                    f"new NOT NULL columns without default: {', '.join(new_required)}"
                )
        out[table] = problems
    for table, read in READ_COLUMNS.items():
        if out.get(table) == ["table missing"]:
            continue
        live = fetch_live_columns(conn, table)
        problems = out.setdefault(table, [])
        if not live:
            problems.append("table missing")
            continue
        missing = [c for c in read if c not in live]
        if missing:
            problems.append(f"missing read columns: {', '.join(missing)}")
    return out


def assert_live_schema(conn) -> None:
    """Raise `SchemaMismatch` if the live DB can't take immy's writes."""
    problems = {t: p for t, p in live_schema_problems(conn).items() if p}
    if not problems:
        return
    version = load_snapshot().get("immich_version", "?")
    lines = [f"  {table}: {'; '.join(p)}" for table, p in problems.items()]
    raise SchemaMismatch(
        f"Immich database schema differs from the one immy writes against "
        f"(Immich {version}):\n" + "\n".join(lines) + "\n"
        "Refusing to write. Check the Immich version, then update "
        "immy/src/immy/schema_contract.py and regenerate the snapshot with "
        "immy/scripts/regen_immich_schema.py."
    )
