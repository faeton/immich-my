"""`immy process <trip>` — Phase Y.1 direct-to-DB asset insert.

For every media file under a trip folder, build an `asset` + `asset_exif`
row pair and insert it into the Immich DB. Uses `checksum = sha1("path:"
+ originalPath)` so rows are idempotent under `(ownerId, libraryId,
checksum)` and won't collide with a future library scan — the scan
computes the same checksum and `ON CONFLICT DO NOTHING` short-circuits.

What Y.1 covers: asset + asset_exif, no derivatives, no job_status. The
trip shows up in the Immich UI with placeholder thumbs; later Y phases
add preview/thumbnail/CLIP/faces.

Container path mapping: `immy process` reads `library.importPaths[0]`
from the DB and anchors every `originalPath` there. The local file
doesn't need to exist on the NAS yet — `asset` rows are self-consistent.
But once Immich's `handleSyncAssets` runs, a missing file flips
`isOffline`, so `immy promote` should rsync before (or in the same run
as) process.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import yaml

from . import derivatives as derivatives_mod
from .derivatives import DerivativeFile
from .exif import ExifRow, MEDIA_EXTS, read_folder
from .pg import LibraryInfo
from .state import AUDIT_DIR


Y_MARKER_FILENAME = "y_processed.yml"

VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts",
    ".insv", ".lrv",
}


def path_checksum(container_path: str) -> bytes:
    """sha1('path:' + abs_path) → 20 raw bytes; matches Immich's sha1Path."""
    return hashlib.sha1(f"path:{container_path}".encode("utf-8")).digest()


def container_path_for(
    media_file: Path,
    trip_folder: Path,
    container_root: str,
) -> str:
    """Map `<trip_folder>/<rel>` on Mac to `<container_root>/<trip>/<rel>`
    in the Immich container. Always POSIX separators, no trailing slash."""
    rel = media_file.relative_to(trip_folder).as_posix()
    root = container_root.rstrip("/")
    return f"{root}/{trip_folder.name}/{rel}"


def asset_type_for(suffix: str) -> str:
    return "VIDEO" if suffix.lower() in VIDEO_EXTS else "IMAGE"


def _parse_exif_datetime(raw: Any) -> datetime | None:
    """Parse ExifTool-style `YYYY:MM:DD HH:MM:SS[±HH:MM]`. Returns tz-aware
    datetime when a zone is present, else naive (caller anchors to UTC)."""
    if not isinstance(raw, str) or len(raw) < 19:
        return None
    s = raw.strip()
    tz = None
    # Optional ±HH:MM suffix (ExifTool's OffsetTime).
    if len(s) >= 25 and s[-6] in "+-" and s[-3] == ":":
        sign = 1 if s[-6] == "+" else -1
        try:
            hours = int(s[-5:-3])
            minutes = int(s[-2:])
        except ValueError:
            return None
        from datetime import timedelta
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
        s = s[:-6]
    try:
        dt = datetime.strptime(s.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if tz is not None else dt


def _best_datetime(row: ExifRow) -> datetime | None:
    for k in (
        "EXIF:DateTimeOriginal",
        "XMP:DateTimeOriginal",
        "QuickTime:CreateDate",
        "EXIF:CreateDate",
    ):
        v = row.get(k)
        parsed = _parse_exif_datetime(v)
        if parsed is not None:
            return parsed
    return None


def _mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """Anchor naive datetimes to UTC so Postgres timestamptz is happy.
    Correct timezone inference is a Y.2+ job — here we only need the column
    to be writable and the UI to not explode."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _float(raw: Any) -> float | None:
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _int(raw: Any) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _str(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


@dataclass
class AssetRow:
    id: str
    device_asset_id: str
    owner_id: str
    library_id: str
    device_id: str
    asset_type: str  # 'IMAGE' | 'VIDEO'
    original_path: str
    original_file_name: str
    checksum: bytes
    file_created_at: datetime
    file_modified_at: datetime
    local_date_time: datetime
    duration: str | None


@dataclass
class AssetExifRow:
    asset_id: str
    description: str
    make: str | None
    model: str | None
    lens_model: str | None
    orientation: str | None
    exif_image_width: int | None
    exif_image_height: int | None
    file_size_in_byte: int | None
    date_time_original: datetime | None
    modify_date: datetime | None
    f_number: float | None
    focal_length: float | None
    iso: int | None
    exposure_time: str | None
    fps: float | None
    latitude: float | None
    longitude: float | None
    time_zone: str | None


def build_rows(
    media_file: Path,
    trip_folder: Path,
    exif_row: ExifRow,
    library: LibraryInfo,
) -> tuple[AssetRow, AssetExifRow]:
    """Produce the asset + asset_exif tuple for one media file.

    Pure: no IO to DB. File `stat()` for mtime + size is the only syscall.
    """
    cpath = container_path_for(media_file, trip_folder, library.container_root)
    checksum = path_checksum(cpath)
    asset_id = str(uuid.uuid4())
    basename = media_file.name
    asset_type = asset_type_for(media_file.suffix)

    mtime_utc = _mtime_utc(media_file)
    best_dt = _best_datetime(exif_row)
    file_created_at = _to_utc(best_dt) if best_dt is not None else mtime_utc
    local_date_time = file_created_at

    duration: str | None = None
    if asset_type == "VIDEO":
        d = _float(exif_row.get("QuickTime:Duration", "Composite:Duration"))
        if d is not None and d > 0:
            h, rem = divmod(d, 3600)
            m, s = divmod(rem, 60)
            duration = f"{int(h):02d}:{int(m):02d}:{s:06.3f}"

    asset = AssetRow(
        id=asset_id,
        device_asset_id=basename.replace(" ", ""),
        owner_id=library.owner_id,
        library_id=library.id,
        device_id="Library Import",
        asset_type=asset_type,
        original_path=cpath,
        original_file_name=basename,
        checksum=checksum,
        file_created_at=file_created_at,
        file_modified_at=mtime_utc,
        local_date_time=local_date_time,
        duration=duration,
    )

    try:
        file_size = media_file.stat().st_size
    except OSError:
        file_size = None

    mod_dt_raw = exif_row.get("EXIF:ModifyDate", "QuickTime:ModifyDate")
    mod_dt_parsed = _parse_exif_datetime(mod_dt_raw)

    exif = AssetExifRow(
        asset_id=asset_id,
        description="",
        make=_str(exif_row.get("EXIF:Make", "QuickTime:Make")),
        model=_str(exif_row.get("EXIF:Model", "QuickTime:Model")),
        lens_model=_str(exif_row.get("EXIF:LensModel", "Composite:LensID")),
        orientation=_str(exif_row.get("EXIF:Orientation")),
        exif_image_width=_int(exif_row.get(
            "EXIF:ExifImageWidth", "EXIF:ImageWidth",
            "QuickTime:ImageWidth", "File:ImageWidth",
        )),
        exif_image_height=_int(exif_row.get(
            "EXIF:ExifImageHeight", "EXIF:ImageHeight",
            "QuickTime:ImageHeight", "File:ImageHeight",
        )),
        file_size_in_byte=file_size,
        date_time_original=_to_utc(best_dt) if best_dt is not None else None,
        modify_date=_to_utc(mod_dt_parsed) if mod_dt_parsed is not None else None,
        f_number=_float(exif_row.get("EXIF:FNumber")),
        focal_length=_float(exif_row.get("EXIF:FocalLength")),
        iso=_int(exif_row.get("EXIF:ISO")),
        exposure_time=_str(exif_row.get("EXIF:ExposureTime")),
        fps=_float(exif_row.get("QuickTime:VideoFrameRate")),
        latitude=_float(exif_row.get(
            "Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude",
        )),
        longitude=_float(exif_row.get(
            "Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude",
        )),
        time_zone=_str(exif_row.get("EXIF:OffsetTimeOriginal", "QuickTime:TimeZone")),
    )
    return asset, exif


# --- INSERT SQL -----------------------------------------------------------

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


def insert_asset(conn: psycopg.Connection, asset: AssetRow, exif: AssetExifRow) -> bool:
    """Insert one asset+exif pair. Returns True if the asset row was newly
    inserted, False on checksum conflict (already in DB).
    """
    with conn.cursor() as cur:
        cur.execute(_INSERT_ASSET, asset.__dict__)
        row = cur.fetchone()
        if row is None:
            return False
        cur.execute(_INSERT_EXIF, exif.__dict__)
    return True


# --- Trip-level driver ----------------------------------------------------


@dataclass
class ProcessResult:
    asset_id: str
    container_path: str
    inserted: bool  # False → already existed (checksum conflict)
    asset_type: str = "IMAGE"  # 'IMAGE' | 'VIDEO'; drives derivatives skip
    derivatives: list[DerivativeFile] | None = None


def process_trip(
    trip_folder: Path,
    conn: psycopg.Connection,
    library: LibraryInfo,
    *,
    compute_derivatives: bool = False,
    on_derivative_error: str = "skip",  # 'skip' | 'raise'
) -> list[ProcessResult]:
    """Read trip folder, insert one asset+exif row per media file, return
    per-file results. Caller is responsible for transaction boundaries —
    we expect a single commit after the list.

    When `compute_derivatives=True`, also generate thumbnail + preview via
    pyvips and stage them under `.audit/derivatives/` using the same
    `thumbs/<userId>/.../<id>_*` layout Immich expects. Staged files are
    local-only — `immy promote` rsyncs them to the NAS and inserts the
    `asset_file` rows. Derivatives are computed for NEWLY inserted IMAGE
    rows; skip path for already-present rows (their thumbs already exist
    on the NAS) and for videos (Y.5).
    """
    rows = read_folder(trip_folder)
    results: list[ProcessResult] = []
    for exif_row in rows:
        asset, exif = build_rows(exif_row.path, trip_folder, exif_row, library)
        inserted = insert_asset(conn, asset, exif)
        derivs: list[DerivativeFile] | None = None
        if compute_derivatives and inserted and asset.asset_type == "IMAGE":
            try:
                derivs = derivatives_mod.compute_for_asset(
                    source_media=exif_row.path,
                    asset_id=asset.id,
                    owner_id=library.owner_id,
                    asset_type=asset.asset_type,
                    trip_folder=trip_folder,
                )
            except Exception:
                if on_derivative_error == "raise":
                    raise
                derivs = None
        results.append(ProcessResult(
            asset_id=asset.id,
            container_path=asset.original_path,
            inserted=inserted,
            asset_type=asset.asset_type,
            derivatives=derivs,
        ))
    return results


def write_marker(trip_folder: Path, results: list[ProcessResult]) -> Path:
    """Drop `.audit/y_processed.yml` so `immy promote` knows to skip the
    library-scan POST (the rows are already there) and to pick up any
    staged derivatives for rsync + `asset_file` INSERTs.

    Marker is the single source of truth between `immy process` (compute)
    and `immy promote` (upload). Extending the schema is safe — promote
    ignores keys it doesn't recognise.
    """
    marker = trip_folder / AUDIT_DIR / Y_MARKER_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    assets = []
    for r in results:
        entry: dict = {
            "file": r.container_path,
            "id": r.asset_id,
            "new": r.inserted,
            "type": r.asset_type,
        }
        if r.derivatives:
            entry["derivatives"] = [
                {
                    "kind": d.kind,
                    "relative_path": d.relative_path,
                    "is_progressive": d.is_progressive,
                    "is_transparent": d.is_transparent,
                }
                for d in r.derivatives
            ]
        assets.append(entry)
    payload = {
        "processed_at": int(time.time()),
        "inserted": sum(1 for r in results if r.inserted),
        "already_present": sum(1 for r in results if not r.inserted),
        "derivatives_staged": sum(
            len(r.derivatives) for r in results if r.derivatives
        ),
        "assets": assets,
    }
    marker.write_text(yaml.safe_dump(payload, sort_keys=False))
    return marker


def read_marker(trip_folder: Path) -> dict | None:
    """Parse `.audit/y_processed.yml`. Returns None if marker is absent."""
    path = marker_path(trip_folder)
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text()) or {}


def marker_path(trip_folder: Path) -> Path:
    return trip_folder / AUDIT_DIR / Y_MARKER_FILENAME


def is_processed(trip_folder: Path) -> bool:
    return marker_path(trip_folder).is_file()


__all__ = [
    "AssetRow", "AssetExifRow", "ProcessResult",
    "build_rows", "path_checksum", "container_path_for", "asset_type_for",
    "insert_asset", "process_trip", "write_marker", "read_marker",
    "is_processed", "marker_path", "Y_MARKER_FILENAME",
]
