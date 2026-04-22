"""Local-first offline mode for `immy process`.

When the tailnet to the NAS Postgres is down (plane, cafe, tunnel
stopped), we still want the expensive work — CLIP, faces, transcripts,
Gemma captions — to happen overnight on battery/AC. All writes that
would go to Postgres are instead cached to `.audit/offline/` inside
the trip folder, then later replayed by `immy sync-offline` when the
DB is reachable.

Design in one paragraph:
- One YAML per asset: `.audit/offline/<sha1_hex>.yml`. File name keyed
  by path-checksum so re-runs find the same entry and the asset UUID
  stays stable across offline runs.
- CLIP embeddings + face embeddings land as sidecar files next to the
  YAML (numpy .npy for CLIP, one-face-per-line .jsonl for faces).
- `process_trip` takes a `Sink` now. `PgSink` is the online path
  (same SQL as before, just moved behind the interface); `OfflineSink`
  is the cache-to-disk path.
- `immy sync-offline <trip>` walks the cache, opens Postgres, replays
  each entry with the same SQL the online path would have used, then
  marks the entry synced so a second sync is a no-op.

Library-info bootstrap: offline mode can't do the `SELECT ... FROM
library` that fetches `ownerId` and `importPaths[0]`. So the first time
`process` runs online we stash LibraryInfo to `~/.immy/library.yml`.
Offline runs read that. If absent, the user is told to run online once.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

import numpy as np
import psycopg
import yaml

from . import pg as pg_mod
from .pg import LibraryInfo
from .state import AUDIT_DIR


OFFLINE_DIR_NAME = "offline"
LIBRARY_CACHE_PATH = Path.home() / ".immy" / "library.yml"


# --- Library info cache ---------------------------------------------------


def cache_library_info(library: LibraryInfo) -> Path:
    """Persist LibraryInfo to `~/.immy/library.yml` so offline runs can
    anchor asset rows without touching the DB. Called after every
    successful online `fetch_library_info` — cheap, idempotent, and the
    user doesn't need to know it happened."""
    LIBRARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_CACHE_PATH.write_text(
        yaml.safe_dump({
            "id": library.id,
            "owner_id": library.owner_id,
            "container_root": library.container_root,
            "cached_at": int(time.time()),
        }, sort_keys=False)
    )
    return LIBRARY_CACHE_PATH


def load_cached_library() -> LibraryInfo | None:
    if not LIBRARY_CACHE_PATH.is_file():
        return None
    data = yaml.safe_load(LIBRARY_CACHE_PATH.read_text()) or {}
    if not all(k in data for k in ("id", "owner_id", "container_root")):
        return None
    return LibraryInfo(
        id=str(data["id"]),
        owner_id=str(data["owner_id"]),
        container_root=str(data["container_root"]),
    )


def derive_container_root_from_marker(trip_folder: Path) -> str | None:
    """Infer Immich's library container_root from an existing
    `.audit/y_processed.yml` by stripping the trip folder name + rel path
    from the first asset's originalPath.

    A fallback for the case where `~/.immy/library.yml` was never cached
    online but at least one trip has been promoted before — we can still
    run offline without bringing the tailnet up. `owner_id` and
    `library_id` stay unresolved; `sync-offline` fills them from DB.
    """
    marker = trip_folder / AUDIT_DIR / "y_processed.yml"
    if not marker.is_file():
        return None
    data = yaml.safe_load(marker.read_text()) or {}
    assets = data.get("assets") or []
    for a in assets:
        file_path = a.get("file")
        if not isinstance(file_path, str):
            continue
        # file looks like: <container_root>/<trip_name>/...
        marker_suffix = f"/{trip_folder.name}/"
        if marker_suffix in file_path:
            return file_path.split(marker_suffix, 1)[0]
    return None


def derive_library_from_any_trip(trips_root: Path) -> LibraryInfo | None:
    """Walk a parent directory (e.g. ~/Media/Trips) looking for any trip
    with a processed marker; return a synthetic LibraryInfo whose
    container_root is correct but owner_id/library_id are placeholders.
    At sync time these placeholders are substituted with real values
    pulled from the live DB, so offline-generated entries stay accurate.
    """
    if not trips_root.is_dir():
        return None
    for sub in sorted(trips_root.iterdir()):
        if not sub.is_dir():
            continue
        root = derive_container_root_from_marker(sub)
        if root:
            return LibraryInfo(
                id="__offline_placeholder__",
                owner_id="__offline_placeholder__",
                container_root=root,
            )
    return None


# --- Sink protocol --------------------------------------------------------


class Sink(Protocol):
    """DB-shaped operations that `process_trip` needs.

    Online (`PgSink`) runs SQL. Offline (`OfflineSink`) serialises to
    `.audit/offline/`. Both must be idempotent on re-run so a second
    pass over an already-processed trip does no useful work.
    """

    def existing_asset_id(self, owner_id: str, library_id: str, checksum: bytes) -> str | None: ...
    def insert_asset_and_exif(self, asset, exif) -> bool: ...
    def caption_info(self, asset_id: str) -> dict | None: ...
    def transcript_info(self, asset_id: str) -> dict | None: ...
    def clip_recorded(self, asset_id: str) -> bool: ...
    def faces_recorded(self, asset_id: str) -> bool: ...
    def update_asset_dims(self, asset_id: str, width: int, height: int) -> None: ...
    def update_asset_duration(self, asset_id: str, duration: str) -> None: ...
    def get_description(self, asset_id: str) -> str | None: ...
    def update_description_if_empty(self, asset_id: str, text: str) -> None: ...
    def update_description_if_ai_or_empty(self, asset_id: str, text: str) -> None: ...
    def clip_dim(self) -> int | None: ...
    def upsert_clip(self, asset_id: str, embedding: list[float], literal: str) -> None: ...
    def replace_faces(self, asset_id: str, width: int, height: int, rows: list[dict]) -> None: ...
    def record_derivatives(self, asset_id: str, derivatives: list[dict]) -> None: ...
    def record_transcript(self, asset_id: str, info: dict) -> None: ...
    def record_caption(self, asset_id: str, info: dict) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


# --- PgSink ---------------------------------------------------------------


# The SQL used to live in process.py; moved here so the Sink owns all
# DB-facing statements and process_trip stays transport-agnostic. Text
# is unchanged so the replay path produces byte-identical rows.

_INSERT_ASSET = """
INSERT INTO asset (
  id, "deviceAssetId", "ownerId", "deviceId", type,
  "originalPath", "originalFileName", checksum, "checksumAlgorithm",
  "fileCreatedAt", "fileModifiedAt", "localDateTime",
  duration, "libraryId", "isExternal"
) VALUES (
  %(id)s, %(device_asset_id)s, %(owner_id)s, %(device_id)s, %(asset_type)s,
  %(original_path)s, %(original_file_name)s, %(checksum)s, 'sha1-path',
  %(file_created_at)s, %(file_modified_at)s, %(local_date_time)s,
  %(duration)s, %(library_id)s, true
)
ON CONFLICT ("ownerId", "libraryId", checksum) WHERE "libraryId" IS NOT NULL
DO NOTHING
RETURNING id
"""

_INSERT_EXIF = """
INSERT INTO asset_exif (
  "assetId", description, make, model, "lensModel", orientation,
  "exifImageWidth", "exifImageHeight", "fileSizeInByte",
  "dateTimeOriginal", "modifyDate",
  "fNumber", "focalLength", iso, "exposureTime", fps,
  latitude, longitude, "timeZone"
) VALUES (
  %(asset_id)s, %(description)s, %(make)s, %(model)s, %(lens_model)s, %(orientation)s,
  %(exif_image_width)s, %(exif_image_height)s, %(file_size_in_byte)s,
  %(date_time_original)s, %(modify_date)s,
  %(f_number)s, %(focal_length)s, %(iso)s, %(exposure_time)s, %(fps)s,
  %(latitude)s, %(longitude)s, %(time_zone)s
)
ON CONFLICT ("assetId") DO NOTHING
"""

_SELECT_EXISTING_ASSET_ID = """
SELECT id FROM asset
WHERE "ownerId" = %(owner_id)s
  AND "libraryId" = %(library_id)s
  AND checksum = %(checksum)s
"""

_UPDATE_ASSET_DIMS = """
UPDATE asset SET width = %(width)s, height = %(height)s WHERE id = %(id)s
"""

_UPDATE_ASSET_DURATION = """
UPDATE asset SET duration = %(duration)s WHERE id = %(id)s
"""

_UPDATE_EXIF_DESCRIPTION_IF_EMPTY = """
UPDATE asset_exif SET description = %(description)s
WHERE "assetId" = %(asset_id)s
  AND (description IS NULL OR description = '')
"""

_UPDATE_EXIF_DESCRIPTION_IF_AI_OR_EMPTY = """
UPDATE asset_exif SET description = %(description)s
WHERE "assetId" = %(asset_id)s
  AND (description IS NULL OR description = '' OR description LIKE 'AI: %%')
"""


class PgSink:
    """Online sink — every method is a SQL statement on the supplied
    connection. Caller owns transaction boundaries (commit/rollback/close)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._clip_dim: int | None = None

    def existing_asset_id(
        self, owner_id: str, library_id: str, checksum: bytes,
    ) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute(_SELECT_EXISTING_ASSET_ID, {
                "owner_id": owner_id, "library_id": library_id,
                "checksum": checksum,
            })
            row = cur.fetchone()
        return str(row[0]) if row else None

    def insert_asset_and_exif(self, asset, exif) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(_INSERT_ASSET, asset.__dict__)
            row = cur.fetchone()
            if row is None:
                # Conflict: resolve existing id so downstream calls hit it.
                cur.execute(_SELECT_EXISTING_ASSET_ID, {
                    "owner_id": asset.owner_id,
                    "library_id": asset.library_id,
                    "checksum": asset.checksum,
                })
                existing = cur.fetchone()
                if existing is not None:
                    asset.id = str(existing[0])
                    exif.asset_id = asset.id
                return False
            cur.execute(_INSERT_EXIF, exif.__dict__)
        return True

    def update_asset_dims(self, asset_id: str, width: int, height: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_UPDATE_ASSET_DIMS, {
                "id": asset_id, "width": width, "height": height,
            })

    def update_asset_duration(self, asset_id: str, duration: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_UPDATE_ASSET_DURATION, {
                "id": asset_id, "duration": duration,
            })

    def get_description(self, asset_id: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute(
                'SELECT description FROM asset_exif WHERE "assetId" = %s',
                (asset_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def update_description_if_empty(self, asset_id: str, text: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                _UPDATE_EXIF_DESCRIPTION_IF_EMPTY,
                {"asset_id": asset_id, "description": text},
            )

    def update_description_if_ai_or_empty(self, asset_id: str, text: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                _UPDATE_EXIF_DESCRIPTION_IF_AI_OR_EMPTY,
                {"asset_id": asset_id, "description": text},
            )

    def clip_dim(self) -> int | None:
        if self._clip_dim is None:
            self._clip_dim = pg_mod.fetch_smart_search_dim(self.conn)
        return self._clip_dim

    def upsert_clip(self, asset_id: str, embedding: list[float], literal: str) -> None:
        pg_mod.upsert_smart_search(self.conn, asset_id, literal)

    def replace_faces(
        self, asset_id: str, width: int, height: int, rows: list[dict],
    ) -> None:
        pg_mod.replace_asset_faces(self.conn, asset_id, width, height, rows)

    # Derivatives / transcript / caption are already on disk; nothing to
    # do in the online sink — the marker file at `.audit/y_processed.yml`
    # is what `immy promote` consumes for rsync + `asset_file` inserts.
    def record_derivatives(self, asset_id: str, derivatives: list[dict]) -> None:
        pass

    def record_transcript(self, asset_id: str, info: dict) -> None:
        pass

    def record_caption(self, asset_id: str, info: dict) -> None:
        pass

    # Resumability queries: online mode already gates CLIP/faces via
    # `inserted=False` on the asset INSERT, and captions/transcripts
    # resume via DB description + sidecar-on-disk. So the PgSink can
    # safely return "not done" for all of these — the downstream code
    # will re-check the DB/disk and make the right call anyway.
    def caption_info(self, asset_id: str) -> dict | None:
        return None

    def transcript_info(self, asset_id: str) -> dict | None:
        return None

    def clip_recorded(self, asset_id: str) -> bool:
        return False

    def faces_recorded(self, asset_id: str) -> bool:
        return False

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        if not self.conn.closed:
            self.conn.close()


# --- OfflineSink ----------------------------------------------------------


def offline_dir(trip_folder: Path) -> Path:
    return trip_folder / AUDIT_DIR / OFFLINE_DIR_NAME


def _entry_path(offline_root: Path, checksum: bytes) -> Path:
    return offline_root / f"{checksum.hex()}.yml"


def _embeddings_dir(offline_root: Path) -> Path:
    return offline_root / "embeddings"


def _load_entry(path: Path) -> dict:
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _dump_entry(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # `default_flow_style=False` and `sort_keys=False` keep the files
    # readable for debugging (a trip dir full of these gets grep'd).
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _serialise_datetime(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _deserialise_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _asset_to_dict(asset) -> dict:
    d = asdict(asset)
    d["checksum"] = asset.checksum.hex()
    for key in ("file_created_at", "file_modified_at", "local_date_time"):
        d[key] = _serialise_datetime(d[key])
    return d


def _exif_to_dict(exif) -> dict:
    d = asdict(exif)
    for key in ("date_time_original", "modify_date"):
        d[key] = _serialise_datetime(d[key])
    return d


class OfflineSink:
    """Offline sink — writes one YAML per asset under `.audit/offline/`.

    Re-run idempotency: each entry is keyed by the path-checksum hex, so
    a second offline pass over the same trip finds the existing file,
    reuses the asset UUID, and overwrites phase sections in place
    (derivatives, clip, faces, transcript, caption). That matches online
    semantics where `ON CONFLICT DO NOTHING` + targeted UPDATEs make the
    second run a no-op at the DB but a fresh compute if ML outputs are
    regenerated.

    The `synced` boolean in each entry gates `immy sync-offline` — once
    an entry replays to Postgres successfully, it's flipped to true and
    a second `sync-offline` skips the row.
    """

    def __init__(
        self,
        trip_folder: Path,
        library: LibraryInfo,
        clip_dim: int | None = None,
    ) -> None:
        self.trip_folder = trip_folder
        self.library = library
        self.root = offline_dir(trip_folder)
        self.root.mkdir(parents=True, exist_ok=True)
        _embeddings_dir(self.root).mkdir(parents=True, exist_ok=True)
        # Bag of (checksum_hex → dict) held open for the duration of one
        # `process_trip` call. We flush to disk per-asset so a Ctrl-C
        # mid-trip still leaves every completed asset persisted.
        self._open: dict[str, dict] = {}
        self._asset_id_to_hex: dict[str, str] = {}
        self._clip_dim = clip_dim

    # --- internal helpers -------------------------------------------

    def _entry_for(self, asset_id: str) -> tuple[str, dict]:
        hex_key = self._asset_id_to_hex[asset_id]
        return hex_key, self._open[hex_key]

    def _flush(self, hex_key: str) -> None:
        _dump_entry(self.root / f"{hex_key}.yml", self._open[hex_key])

    # --- Sink interface --------------------------------------------

    def existing_asset_id(
        self, owner_id: str, library_id: str, checksum: bytes,
    ) -> str | None:
        existing = _load_entry(_entry_path(self.root, checksum))
        if not existing:
            return None
        return str(existing.get("asset", {}).get("id")) or None

    def insert_asset_and_exif(self, asset, exif) -> bool:
        hex_key = asset.checksum.hex()
        path = self.root / f"{hex_key}.yml"
        existing = _load_entry(path)
        if existing.get("asset", {}).get("id"):
            # Already captured: reuse uuid so the rest of the pipeline
            # keeps writing into the same entry across offline re-runs.
            asset.id = str(existing["asset"]["id"])
            exif.asset_id = asset.id
            self._open[hex_key] = existing
            self._asset_id_to_hex[asset.id] = hex_key
            return False
        entry = {
            "schema": 1,
            "synced": False,
            "created_at": int(time.time()),
            "library_id": self.library.id,
            "owner_id": self.library.owner_id,
            "asset": _asset_to_dict(asset),
            "exif": _exif_to_dict(exif),
        }
        self._open[hex_key] = entry
        self._asset_id_to_hex[asset.id] = hex_key
        _dump_entry(path, entry)
        return True

    def update_asset_dims(self, asset_id: str, width: int, height: int) -> None:
        hex_key, entry = self._entry_for(asset_id)
        entry["asset"]["width"] = width
        entry["asset"]["height"] = height
        self._flush(hex_key)

    def update_asset_duration(self, asset_id: str, duration: str) -> None:
        hex_key, entry = self._entry_for(asset_id)
        entry["asset"]["duration"] = duration
        self._flush(hex_key)

    def get_description(self, asset_id: str) -> str | None:
        # In offline mode there is no user-typed description to defer to
        # — the cached entry only has what we wrote. Returning None makes
        # the caption + transcript paths proceed with their writes; the
        # server-side `LIKE 'AI: %'` guard at sync time still protects
        # user descriptions that appeared in the meantime.
        if asset_id not in self._asset_id_to_hex:
            return None
        _, entry = self._entry_for(asset_id)
        return entry.get("exif", {}).get("description") or None

    def update_description_if_empty(self, asset_id: str, text: str) -> None:
        hex_key, entry = self._entry_for(asset_id)
        current = entry.get("exif", {}).get("description") or ""
        if not current:
            entry["exif"]["description"] = text
            self._flush(hex_key)

    def update_description_if_ai_or_empty(self, asset_id: str, text: str) -> None:
        hex_key, entry = self._entry_for(asset_id)
        current = entry.get("exif", {}).get("description") or ""
        if not current or current.startswith("AI: "):
            entry["exif"]["description"] = text
            self._flush(hex_key)

    def clip_dim(self) -> int | None:
        return self._clip_dim

    def upsert_clip(
        self, asset_id: str, embedding: list[float], literal: str,
    ) -> None:
        hex_key, entry = self._entry_for(asset_id)
        emb_path = _embeddings_dir(self.root) / f"{hex_key}.clip.npy"
        np.save(emb_path, np.asarray(embedding, dtype=np.float32))
        entry["clip"] = {
            "dim": len(embedding),
            "path": emb_path.relative_to(self.root).as_posix(),
        }
        self._flush(hex_key)

    def replace_faces(
        self, asset_id: str, width: int, height: int, rows: list[dict],
    ) -> None:
        hex_key, entry = self._entry_for(asset_id)
        faces_path = _embeddings_dir(self.root) / f"{hex_key}.faces.jsonl"
        # Drop the pgvector text literal in favour of the raw float list;
        # we regenerate the literal at sync time from the numpy array.
        records = []
        with faces_path.open("w") as fh:
            for row in rows:
                serialised = {
                    "id": row["id"],
                    "x1": row["x1"], "y1": row["y1"],
                    "x2": row["x2"], "y2": row["y2"],
                    "embedding": row["embedding"],  # kept as pgvector literal
                }
                fh.write(json.dumps(serialised) + "\n")
                records.append({"id": row["id"]})
        entry["faces"] = {
            "count": len(rows),
            "width": width,
            "height": height,
            "path": faces_path.relative_to(self.root).as_posix(),
        }
        self._flush(hex_key)

    def record_derivatives(self, asset_id: str, derivatives: list[dict]) -> None:
        hex_key, entry = self._entry_for(asset_id)
        entry["derivatives"] = derivatives
        self._flush(hex_key)

    def record_transcript(self, asset_id: str, info: dict) -> None:
        hex_key, entry = self._entry_for(asset_id)
        entry["transcript"] = info
        self._flush(hex_key)

    def record_caption(self, asset_id: str, info: dict) -> None:
        hex_key, entry = self._entry_for(asset_id)
        entry["caption"] = info
        self._flush(hex_key)

    # Resumability queries — offline mode's whole reason for existence.
    # These read the persisted YAML entry so a Ctrl-C'd overnight run
    # can be resumed without redoing per-file work that already succeeded.
    def _entry_or_none(self, asset_id: str) -> dict | None:
        hex_key = self._asset_id_to_hex.get(asset_id)
        if hex_key is None:
            return None
        return self._open.get(hex_key)

    def caption_info(self, asset_id: str) -> dict | None:
        entry = self._entry_or_none(asset_id)
        return entry.get("caption") if entry else None

    def transcript_info(self, asset_id: str) -> dict | None:
        entry = self._entry_or_none(asset_id)
        return entry.get("transcript") if entry else None

    def clip_recorded(self, asset_id: str) -> bool:
        entry = self._entry_or_none(asset_id)
        return bool(entry and entry.get("clip"))

    def faces_recorded(self, asset_id: str) -> bool:
        entry = self._entry_or_none(asset_id)
        return bool(entry and entry.get("faces"))

    def commit(self) -> None:
        # Entries were already flushed per-phase; nothing further needed.
        pass

    def rollback(self) -> None:
        # Offline mode has no transactional boundary — per-phase writes
        # mean a mid-trip crash leaves partial results on disk, which is
        # exactly what we want (next run resumes).
        pass

    def close(self) -> None:
        pass


# --- Sync (offline → DB) --------------------------------------------------


def iter_entries(trip_folder: Path) -> Iterator[tuple[Path, dict]]:
    root = offline_dir(trip_folder)
    if not root.is_dir():
        return
    for yml in sorted(root.glob("*.yml")):
        data = yaml.safe_load(yml.read_text()) or {}
        if not data:
            continue
        yield yml, data


def _pgvector_literal_from_npy(path: Path) -> tuple[list[float], str]:
    arr = np.load(path)
    floats = arr.astype(float).tolist()
    literal = "[" + ",".join(f"{v:.8f}" for v in floats) + "]"
    return floats, literal


def sync_trip(
    trip_folder: Path,
    conn: psycopg.Connection,
    *,
    library: LibraryInfo | None = None,
    progress: Any = None,
) -> dict:
    """Replay every unsynced `.audit/offline/*.yml` entry into Postgres.

    `library` is the live library info fetched from the DB at sync time —
    used to substitute placeholder `owner_id`/`library_id` values that
    the offline path stamped into cached entries when the DB was down
    and only `container_root` could be recovered from existing markers.

    Returns a summary dict suitable for display. Each entry is processed
    in its own transaction — a DB-level failure on one asset rolls back
    only that asset and the rest of the trip keeps going.
    """
    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    synced = 0
    skipped = 0
    failed = 0
    entries = list(iter_entries(trip_folder))
    _emit(f"sync-offline: {len(entries)} entry(ies) to consider")

    for idx, (yml_path, data) in enumerate(entries, start=1):
        if data.get("synced"):
            skipped += 1
            continue
        rel = yml_path.name
        _emit(f"[{idx}/{len(entries)}] {rel}")
        try:
            _replay_entry(conn, trip_folder, data, library=library)
            data["synced"] = True
            data["synced_at"] = int(time.time())
            _dump_entry(yml_path, data)
            conn.commit()
            synced += 1
            _emit(f"    → synced")
        except Exception as exc:
            conn.rollback()
            failed += 1
            _emit(f"    → FAILED: {exc}")
    return {
        "total": len(entries),
        "synced": synced,
        "skipped": skipped,
        "failed": failed,
    }


_OFFLINE_PLACEHOLDER = "__offline_placeholder__"


def _replay_entry(
    conn: psycopg.Connection,
    trip_folder: Path,
    data: dict,
    *,
    library: LibraryInfo | None = None,
) -> None:
    """Replay one cached asset into Postgres. All writes idempotent via
    the same ON CONFLICT / LIKE-guarded UPDATE pattern the online path
    uses, so a partial success → retry → completion sequence is safe."""
    asset_raw = data["asset"]
    exif_raw = data["exif"]

    asset_params = dict(asset_raw)
    asset_params["checksum"] = bytes.fromhex(asset_raw["checksum"])
    for key in ("file_created_at", "file_modified_at", "local_date_time"):
        asset_params[key] = _deserialise_datetime(asset_params.get(key))
    # Placeholder substitution: offline mode without a cached library
    # stamped `__offline_placeholder__` for owner_id / library_id. Fill
    # them from the live `library` we fetched at sync time.
    if library is not None:
        if asset_params.get("owner_id") == _OFFLINE_PLACEHOLDER:
            asset_params["owner_id"] = library.owner_id
        if asset_params.get("library_id") == _OFFLINE_PLACEHOLDER:
            asset_params["library_id"] = library.id
    exif_params = dict(exif_raw)
    for key in ("date_time_original", "modify_date"):
        exif_params[key] = _deserialise_datetime(exif_params.get(key))

    with conn.cursor() as cur:
        cur.execute(_INSERT_ASSET, asset_params)
        row = cur.fetchone()
        if row is None:
            # Already in DB — resolve existing id and retarget exif/clip
            # /faces to that canonical uuid so we don't orphan them.
            cur.execute(_SELECT_EXISTING_ASSET_ID, {
                "owner_id": asset_params["owner_id"],
                "library_id": asset_params["library_id"],
                "checksum": asset_params["checksum"],
            })
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("insert conflict but no existing row found")
            asset_id = str(existing[0])
        else:
            asset_id = str(row[0])
            exif_params["asset_id"] = asset_id
            cur.execute(_INSERT_EXIF, exif_params)

        width = asset_params.get("width")
        height = asset_params.get("height")
        if width and height:
            cur.execute(_UPDATE_ASSET_DIMS, {
                "id": asset_id, "width": width, "height": height,
            })
        duration = asset_params.get("duration")
        if duration:
            cur.execute(_UPDATE_ASSET_DURATION, {
                "id": asset_id, "duration": duration,
            })
        description = exif_raw.get("description")
        if description:
            # Captions land via AI-only guard; transcripts land via empty
            # guard. We can't distinguish perfectly at replay time, so
            # prefer the AI-or-empty variant — it's a superset of empty.
            cur.execute(_UPDATE_EXIF_DESCRIPTION_IF_AI_OR_EMPTY, {
                "asset_id": asset_id, "description": description,
            })

    offline_root = offline_dir(trip_folder)
    clip = data.get("clip")
    if clip:
        clip_path = offline_root / clip["path"]
        _, literal = _pgvector_literal_from_npy(clip_path)
        pg_mod.upsert_smart_search(conn, asset_id, literal)

    faces = data.get("faces")
    if faces and faces.get("count"):
        faces_path = offline_root / faces["path"]
        rows = []
        with faces_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                rows.append({
                    "id": rec["id"],
                    "x1": rec["x1"], "y1": rec["y1"],
                    "x2": rec["x2"], "y2": rec["y2"],
                    "embedding": rec["embedding"],
                })
        if rows:
            pg_mod.replace_asset_faces(
                conn, asset_id, faces["width"], faces["height"], rows,
            )


__all__ = [
    "Sink", "PgSink", "OfflineSink",
    "cache_library_info", "load_cached_library", "LIBRARY_CACHE_PATH",
    "offline_dir", "iter_entries", "sync_trip",
]
