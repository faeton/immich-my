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


def like_prefix(prefix: str) -> str:
    """A LIKE pattern matching strings that start with `prefix` literally.

    `_` and `%` are LIKE wildcards and both are legal in trip folder names
    (`2024_06-trip` would otherwise also match `2024X06-trip/...`), so they
    — and the escape char itself — are backslash-escaped. Use with
    `LIKE %s ESCAPE '\\'`.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


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


# Immich 3.0.2 `config.ts` default for machineLearning.clip.modelName (also
# what the live DB implies: no override in system-config, smart_search is
# vector(512)). Used when system-config carries no explicit model.
IMMICH_DEFAULT_CLIP_MODEL = "ViT-B-32__openai"

_QUERY_IMMICH_CLIP_MODEL = (
    "SELECT value #>> '{machineLearning,clip,modelName}' "
    "FROM system_metadata WHERE key = 'system-config'"
)


def fetch_immich_clip_model(conn: psycopg.Connection) -> str:
    """Immich's configured CLIP model (read-only): `system_metadata`
    `system-config` → machineLearning.clip.modelName, else Immich's default."""
    row = conn.execute(_QUERY_IMMICH_CLIP_MODEL).fetchone()
    name = row[0] if row else None
    return str(name) if name else IMMICH_DEFAULT_CLIP_MODEL


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

# Rows carrying a `personId` are Immich's face→person links (clustered or
# named by the user) — the bulk of ML faces in a live library. They are never
# deleted; only unassigned ML detections are replaced.
_SELECT_ASSIGNED_FACE_BOXES = """
SELECT "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2",
       "imageWidth", "imageHeight"
FROM asset_face
WHERE "assetId" = %(asset_id)s AND "personId" IS NOT NULL
"""

_DELETE_UNASSIGNED_ML_FACES = """
DELETE FROM asset_face
WHERE "assetId" = %(asset_id)s AND "sourceType" = 'machine-learning'
  AND "personId" IS NULL
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

# A new detection whose box overlaps a kept person-assigned face at least this
# much (intersection-over-union) is the same face found again — skipped, so
# the person keeps exactly one face there.
SAME_FACE_IOU = 0.5

_Box = tuple[float, float, float, float]


def _usable_size(width, height) -> bool:
    return bool(width and height and width > 0 and height > 0)


def _normalized_box(x1, y1, x2, y2, width, height) -> _Box:
    """Bbox in 0..1 image units. Caller guarantees a usable size."""
    return (x1 / width, y1 / height, x2 / width, y2 / height)


def _box_iou(a: _Box, b: _Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def replace_asset_faces(
    conn: psycopg.Connection,
    asset_id: str,
    image_width: int,
    image_height: int,
    faces: list[dict],
) -> int:
    """Replace the unassigned ML-detected faces for one asset.

    Faces linked to a person (`personId` set — Immich's clustering or a
    user's naming) are never touched: deleting them would orphan the
    person. Only `sourceType='machine-learning'` rows with no `personId` are
    deleted (CASCADE wipes their `face_search` too). Each face in `faces` is
    then inserted with its 512-dim ArcFace embedding — except one whose box
    overlaps a kept person-assigned face at IoU >= `SAME_FACE_IOU` (compared
    in normalized coordinates; a kept row stored without a size uses this
    detection's size, and an overlap that can't be measured at all counts
    as a match), which is the same face re-detected. User-
    tagged faces (`sourceType='exif'`) are untouched. Idempotent —
    re-running `immy process` with `--with-faces` regenerates the rows.

    Each face dict must carry: `id` (new uuid), `x1`, `y1`, `x2`, `y2`,
    and `embedding` (pgvector text literal). Returns the number of faces
    inserted.
    """
    written = 0
    with conn.cursor() as cur:
        cur.execute(_SELECT_ASSIGNED_FACE_BOXES, {"asset_id": asset_id})
        # Kept boxes in 0..1 units. A row stored without a size (0) is
        # placed in this detection's frame — same asset, so the best
        # reference there is. `None` = overlap can't be measured at all.
        detection_sized = _usable_size(image_width, image_height)
        assigned: list[_Box | None] = []
        for x1, y1, x2, y2, w, h in cur.fetchall():
            if not _usable_size(w, h):
                w, h = image_width, image_height
            assigned.append(
                _normalized_box(x1, y1, x2, y2, w, h)
                if detection_sized and _usable_size(w, h) else None
            )
        cur.execute(_DELETE_UNASSIGNED_ML_FACES, {"asset_id": asset_id})
        for face in faces:
            box = _normalized_box(
                face["x1"], face["y1"], face["x2"], face["y2"],
                image_width, image_height,
            ) if detection_sized else None
            # Unmeasurable overlap counts as the same face: never risk a
            # duplicate beside a person's face.
            if any(
                box is None or kept is None or _box_iou(box, kept) >= SAME_FACE_IOU
                for kept in assigned
            ):
                continue
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
            written += 1
    return written


# --- apple-people: naming Immich's existing (unnamed) face clusters ------

_SELECT_EXISTING_FACES = """
SELECT af.id, af."assetId", af."personId", p.name,
       af."boundingBoxX1"::float / af."imageWidth",
       af."boundingBoxY1"::float / af."imageHeight",
       af."boundingBoxX2"::float / af."imageWidth",
       af."boundingBoxY2"::float / af."imageHeight"
FROM asset_face af
LEFT JOIN person p ON p.id = af."personId"
WHERE af."assetId" = ANY(%(asset_ids)s)
  AND af."imageWidth" > 0 AND af."imageHeight" > 0
"""


def fetch_existing_faces(
    conn: psycopg.Connection, asset_ids: list[str],
) -> dict[str, list[tuple[str, str | None, str | None, float, float, float, float]]]:
    """Batch-fetch `asset_face` rows for the given assets, bbox normalized
    to 0..1. Returns `assetId -> [(face_id, person_id, person_name, x1, y1,
    x2, y2), ...]`. Caller (`apple_photos.build_person_plans`) does the
    overlap logic — this is IO only.
    """
    out: dict[str, list[tuple]] = {}
    if not asset_ids:
        return out
    rows = conn.execute(_SELECT_EXISTING_FACES, {"asset_ids": asset_ids}).fetchall()
    for face_id, asset_id, person_id, name, x1, y1, x2, y2 in rows:
        out.setdefault(str(asset_id), []).append(
            (str(face_id), str(person_id) if person_id else None, name, x1, y1, x2, y2)
        )
    return out


_NAME_PERSON = """
UPDATE person SET name = %(name)s
WHERE id = %(person_id)s AND name = ''
"""


def name_person(conn: psycopg.Connection, person_id: str, name: str) -> bool:
    """Set a currently-unnamed person's name. Guarded by `name = ''` in the
    WHERE clause so this never clobbers an existing name (e.g. a race with
    the user naming it in the Immich UI between preview and apply).
    Returns whether a row was actually updated.
    """
    with conn.cursor() as cur:
        cur.execute(_NAME_PERSON, {"person_id": person_id, "name": name})
        return cur.rowcount > 0


_ATTACH_ORPHAN_FACES = """
UPDATE asset_face SET "personId" = %(person_id)s
WHERE id = ANY(%(face_ids)s) AND "personId" IS NULL
"""


def attach_orphan_faces(
    conn: psycopg.Connection, face_ids: list[str], person_id: str,
) -> int:
    """Attach unclustered `asset_face` rows to a person. Guarded by
    `personId IS NULL` so an already-clustered face is never reassigned.
    """
    if not face_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(_ATTACH_ORPHAN_FACES, {"face_ids": face_ids, "person_id": person_id})
        return cur.rowcount
