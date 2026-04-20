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


# --- smart_search (Y.3 CLIP) ---------------------------------------------

# pgvector exposes its configured dimension via `format_type(atttypid,
# atttypmod)`, which returns literals like `vector(512)`. Parsing that is
# more robust than reading `atttypmod` directly (the raw mod is pgvector-
# version-specific; the formatted string is stable). If the `embedding`
# column is untyped vector (no mod), `format_type` returns `vector` and
# we return None so the caller can surface a clear error.
_QUERY_SMART_SEARCH_DIM = """
SELECT format_type(atttypid, atttypmod)
FROM pg_attribute
WHERE attrelid = 'smart_search'::regclass
  AND attname = 'embedding'
  AND NOT attisdropped
"""


def fetch_smart_search_dim(conn: psycopg.Connection) -> int | None:
    """Return the configured `smart_search.embedding` dimension, or None if
    the column has no declared dimension (unusual — Immich always sets one).

    Immich's `SmartInfoService.onConfigUpdate` calls `ALTER TABLE` when the
    CLIP model changes, so the dimension can shift between minor versions.
    We query it once per run and assert our embedding matches.
    """
    row = conn.execute(_QUERY_SMART_SEARCH_DIM).fetchone()
    if row is None:
        raise LookupError("smart_search.embedding column not found")
    formatted = str(row[0])  # e.g. 'vector(512)'
    if "(" not in formatted or ")" not in formatted:
        return None
    inner = formatted.split("(", 1)[1].rstrip(")")
    try:
        return int(inner)
    except ValueError:
        return None


_UPSERT_SMART_SEARCH = """
INSERT INTO smart_search ("assetId", embedding)
VALUES (%(asset_id)s, %(embedding)s::vector)
ON CONFLICT ("assetId")
DO UPDATE SET embedding = EXCLUDED.embedding
"""


def upsert_smart_search(
    conn: psycopg.Connection, asset_id: str, embedding_literal: str,
) -> None:
    """Upsert a CLIP embedding for one asset. `embedding_literal` is the
    pgvector text form (see `clip.to_pgvector_literal`); pgvector does the
    cast to `vector(N)` server-side.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPSERT_SMART_SEARCH,
            {"asset_id": asset_id, "embedding": embedding_literal},
        )


# --- asset_face + face_search (Y.4) --------------------------------------

_DELETE_FACES_FOR_ASSET = """
DELETE FROM asset_face
WHERE "assetId" = %(asset_id)s AND "sourceType" = 'machine-learning'
"""

_INSERT_ASSET_FACE = """
INSERT INTO asset_face (
  id, "assetId", "imageWidth", "imageHeight",
  "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
  "sourceType", "isVisible"
) VALUES (
  %(id)s, %(asset_id)s, %(image_width)s, %(image_height)s,
  %(x1)s, %(y1)s, %(x2)s, %(y2)s,
  'machine-learning', true
)
"""

_INSERT_FACE_SEARCH = """
INSERT INTO face_search ("faceId", embedding)
VALUES (%(face_id)s, %(embedding)s::vector)
"""


def replace_asset_faces(
    conn: psycopg.Connection,
    asset_id: str,
    image_width: int,
    image_height: int,
    faces: list[dict],
) -> int:
    """Replace the ML-detected faces for one asset.

    Any existing `asset_face` rows with `sourceType='machine-learning'`
    for this asset are deleted (CASCADE wipes their `face_search` too),
    then every face in `faces` is inserted fresh along with its 512-dim
    ArcFace embedding. User-tagged faces (`sourceType='exif'`) are
    untouched. Idempotent — re-running `immy process` with `--with-faces`
    regenerates the rows.

    Each face dict must carry: `id` (new uuid), `x1`, `y1`, `x2`, `y2`,
    and `embedding` (pgvector text literal).
    """
    with conn.cursor() as cur:
        cur.execute(_DELETE_FACES_FOR_ASSET, {"asset_id": asset_id})
        for face in faces:
            cur.execute(_INSERT_ASSET_FACE, {
                "id": face["id"],
                "asset_id": asset_id,
                "image_width": image_width,
                "image_height": image_height,
                "x1": face["x1"], "y1": face["y1"],
                "x2": face["x2"], "y2": face["y2"],
            })
            cur.execute(_INSERT_FACE_SEARCH, {
                "face_id": face["id"],
                "embedding": face["embedding"],
            })
    return len(faces)
