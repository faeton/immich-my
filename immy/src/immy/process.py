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
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
import yaml

from . import captions as captions_mod
from . import clip as clip_mod
from . import derivatives as derivatives_mod
from . import dji as dji_mod
from . import faces as faces_mod
from . import insta360 as insta360_mod
from . import journal as journal_mod
from . import offline as offline_mod
from . import pg as pg_mod
from . import raw as raw_mod
from . import transcripts as transcripts_mod
from .derivatives import DerivativeFile
from .exif import ExifRow, MEDIA_EXTS, read_folder
from .journal import Journal
from .offline import Sink
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
    datetime when a zone is present, else naive (caller anchors to UTC).

    Rejects plausibly-valid-but-nonsensical dates: cameras sometimes emit
    `0000:00:00 00:00:00` (a literal placeholder, not a real moment), and
    a few write "1904:01:01" as the Mac epoch. We treat anything before
    1970 or after 2100 as missing so `_best_datetime` keeps looking and
    the filename-date rule can fire cleanly.
    """
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
    if dt.year < 1970 or dt.year > 2100:
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
    # Populated after derivative gen (Y.2). Written via UPDATE, not the
    # initial INSERT, because we don't decode the image until derivatives
    # run. Immich's viewer reads these for intrinsic fullscreen dims.
    width: int | None = None
    height: int | None = None


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

_SELECT_EXISTING_ASSET_ID = """
SELECT id FROM asset
WHERE "ownerId" = %(owner_id)s
  AND "libraryId" = %(library_id)s
  AND checksum = %(checksum)s
"""

_UPDATE_ASSET_DIMS = """
UPDATE asset
SET width = %(width)s, height = %(height)s
WHERE id = %(id)s
"""

_UPDATE_ASSET_DURATION = """
UPDATE asset
SET duration = %(duration)s
WHERE id = %(id)s
"""

_UPDATE_EXIF_DESCRIPTION = """
UPDATE asset_exif
SET description = %(description)s
WHERE "assetId" = %(asset_id)s
"""

_UPDATE_EXIF_DESCRIPTION_IF_EMPTY = """
UPDATE asset_exif
SET description = %(description)s
WHERE "assetId" = %(asset_id)s
  AND (description IS NULL OR description = '')
"""

# For the captioner: overwrite only when empty or already AI-prefixed
# (i.e. a prior caption run). User-typed descriptions are never touched.
# The LIKE is intentionally cheap and anchored — `captions.AI_PREFIX`
# is fixed at `'AI: '`, so the pattern is stable across versions.
_UPDATE_EXIF_DESCRIPTION_IF_AI = """
UPDATE asset_exif
SET description = %(description)s
WHERE "assetId" = %(asset_id)s
  AND (description IS NULL OR description = '' OR description LIKE 'AI: %%')
"""


def update_asset_dimensions(
    conn: psycopg.Connection, asset_id: str, width: int, height: int,
) -> None:
    """Set `asset.width`/`asset.height` after derivative gen.

    Immich's web viewer reads these for intrinsic layout dims; leaving
    them NULL makes fullscreen render in a tiny letterboxed box. We
    split this from the initial INSERT because dims require decoding
    the image (via libvips in `derivatives.compute_for_asset`), and
    we only want to pay that decode cost when `compute_derivatives=True`.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_ASSET_DIMS,
            {"id": asset_id, "width": width, "height": height},
        )


def update_exif_description(
    conn: psycopg.Connection, asset_id: str, description: str,
) -> None:
    """Write a description excerpt into `asset_exif.description`. Used by
    the transcript path — the full `.srt` sidecar lives on disk next to
    the source video; the DB only gets the searchable plain-text excerpt."""
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_EXIF_DESCRIPTION,
            {"asset_id": asset_id, "description": description},
        )


def update_asset_duration(
    conn: psycopg.Connection, asset_id: str, duration: str,
) -> None:
    """Overwrite `asset.duration` with the ffprobe value.

    The initial INSERT guesses duration from `QuickTime:Duration` when
    ExifTool surfaces it, but some containers (.mts, .avi, repaired
    .mp4s, DJI `.lrv` proxies) either lack the tag or emit a wrong
    one. ffprobe reads the actual container, so once derivatives run
    we let it win.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE_ASSET_DURATION,
            {"id": asset_id, "duration": duration},
        )


def insert_asset(conn: psycopg.Connection, asset: AssetRow, exif: AssetExifRow) -> bool:
    """Insert one asset+exif pair. Returns True if the asset row was newly
    inserted, False on checksum conflict (already in DB).

    On conflict, resolve the existing row's id and mutate `asset.id` and
    `exif.asset_id` in place so downstream code (marker writes,
    derivative `asset_file` inserts) references the real asset — not the
    ghost UUID `build_rows` generated speculatively.
    """
    with conn.cursor() as cur:
        cur.execute(_INSERT_ASSET, asset.__dict__)
        row = cur.fetchone()
        if row is None:
            cur.execute(_SELECT_EXISTING_ASSET_ID, {
                "owner_id": asset.owner_id,
                "library_id": asset.library_id,
                "checksum": asset.checksum,
            })
            existing = cur.fetchone()
            if existing is not None:
                # psycopg returns `uuid.UUID` for uuid columns; downstream
                # code (marker YAML serialization, path construction) needs
                # a plain str.
                existing_id = str(existing[0])
                asset.id = existing_id
                exif.asset_id = existing_id
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
    clip_embedded: bool = False  # True → smart_search row upserted this run
    faces_detected: int = 0  # count of asset_face rows written this run
    transcript: dict | None = None  # {"path": str, "language": str} or None
    caption: dict | None = None  # {"text": str, "model": str, "prompt_tokens", "completion_tokens"}


def process_trip(
    trip_folder: Path,
    conn: psycopg.Connection | None,
    library: LibraryInfo,
    *,
    sink: Sink | None = None,
    compute_derivatives: bool = False,
    compute_clip: bool = False,
    compute_faces: bool = False,
    compute_transcripts: bool = False,
    compute_captions: bool = False,
    recaption: bool = False,
    captioner_config: captions_mod.CaptionerConfig | None = None,
    transcode_videos: bool = True,
    clip_model: str = clip_mod.DEFAULT_MODEL,
    faces_model: str = faces_mod.DEFAULT_MODEL,
    transcript_model: str = transcripts_mod.DEFAULT_MODEL,
    transcript_prompt: str | None = None,
    on_derivative_error: str = "skip",  # 'skip' | 'raise'
    on_clip_error: str = "skip",        # 'skip' | 'raise'
    on_faces_error: str = "skip",       # 'skip' | 'raise'
    on_transcript_error: str = "skip",  # 'skip' | 'raise'
    on_caption_error: str = "skip",     # 'skip' | 'raise'
    progress: Callable[[str], None] | None = None,
    journal: Journal | None = None,
    commit_per_asset: bool = True,
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

    When `compute_clip=True`, compute a CLIP embedding from the just-staged
    preview (requires `compute_derivatives=True` — we feed the preview file,
    matching Immich's own pipeline) and UPSERT the `smart_search` row in the
    same transaction. Skipped for videos and for already-present assets
    (Immich's own worker or a prior run owns that row). CLIP dim is verified
    once up-front against `smart_search.embedding` typmod.
    """
    rows = read_folder(trip_folder)

    # DJI drones write every clip as a paired `.MP4` master + `.LRF`
    # low-res proxy sharing a stem. The LRF is not user-visible content
    # — it's there to accelerate ffmpeg decode of the master. Drop
    # paired LRFs from ingest entirely; unpaired orphans fall through
    # (the user should see them as normal so they can investigate).
    dji_proxy_index = dji_mod.build_proxy_index(r.path for r in rows)
    rows = [r for r in rows if not dji_mod.is_paired_proxy(r.path, dji_proxy_index)]

    # Cameras in RAW+JPEG mode (DJI Mavic, Sony α, Canon, Nikon, Fuji)
    # write `<stem>.DNG/ARW/CR3/…` plus `<stem>.JPG` — the JPG is the
    # in-camera preview derived from the RAW. Drop the paired JPEG; the
    # RAW carries full sensor data + the same EXIF/GPS and is the real
    # asset. Unpaired JPGs (phone photos, exports) ingest normally.
    raw_index = raw_mod.build_raw_index(r.path for r in rows)
    rows = [r for r in rows if not raw_mod.is_paired_preview(r.path, raw_index)]

    results: list[ProcessResult] = []

    # Sink routes every would-be DB write. Default is the online PgSink
    # wrapping the caller's connection; `immy process --offline` passes
    # an OfflineSink and conn=None. Tests that pass a MagicMock conn
    # still work — PgSink's methods go through `conn.cursor()`.
    if sink is None:
        if conn is None:
            raise ValueError("process_trip requires either `conn` or `sink`")
        sink = offline_mod.PgSink(conn)

    expected_dim: int | None = None
    if compute_clip:
        if not compute_derivatives:
            raise ValueError("compute_clip requires compute_derivatives=True")
        expected_dim = sink.clip_dim()

    # Journal anchors per-phase resumability across crashes / Ctrl-C.
    # On every successful asset insert we drop an "ingest" entry; that
    # entry is what later runs use to know "we own this asset" once the
    # ON CONFLICT path returns inserted=False on resume. Without it the
    # enrichers would skip a half-finished asset because they currently
    # gate on `inserted`.
    if journal is None:
        journal = Journal.load(trip_folder)
    INGEST_VERSION = "v1"
    DERIV_VERSION = journal_mod.DERIVATIVES_VERSION
    CLIP_VERSION = journal_mod.clip_version(clip_model)
    FACES_VERSION = journal_mod.faces_version(faces_model)
    TRANSCRIPT_VERSION = journal_mod.transcript_version(transcript_model)
    CAPTION_VERSION = (
        journal_mod.caption_version(captioner_config.model)
        if captioner_config is not None else "caption:none"
    )

    # Per-file progress is opt-in via the `progress` callback. Callers
    # that want a live counter (CLI / batch scripts) pass `console.print`;
    # tests and library callers leave it None to stay silent. Each asset
    # emits a header line on start and one summary line at end with
    # per-phase wall-clock timings — enough to tell "still making
    # derivatives" from "stuck on Whisper" without parsing anything.
    total = len(rows)

    # Insta360 produces one `VID_*.insv` master per lens (5.7K+ dual-
    # fisheye, multi-GB) and one `LRV_*.lrv` low-res proxy alongside.
    # Decoding the proxy for the poster + encoded_video derivative is
    # ~30× faster and produces an identical-looking dual-fisheye tile.
    # Asset rows still point at the real master — only ffmpeg's input
    # changes.
    proxy_index = insta360_mod.build_proxy_index(r.path for r in rows)
    # Insta360 masters come in lens pairs (_00_ / _10_) that share one
    # LRV proxy. Both produce byte-identical derivatives, so we keep the
    # first sibling's `DerivativeResult` and hardlink it onto the
    # second's asset paths instead of re-running ffmpeg. Keyed by proxy
    # Path; DJI proxies are 1:1 (no siblings) so they never hit twice.
    proxy_deriv_cache: dict[Path, "derivatives_mod.DerivativeResult"] = {}

    def _emit(msg: str) -> None:
        if progress is not None:
            progress(msg)

    def _phase(fn, label: str, timings: dict) -> Any:
        t0 = time.monotonic()
        try:
            return fn()
        finally:
            timings[label] = time.monotonic() - t0

    for idx, exif_row in enumerate(rows, start=1):
        timings: dict[str, float] = {}
        asset_t0 = time.monotonic()

        # Header line: printed before any work so the user sees which
        # file is in progress while it's still running. Size helps when
        # a single 5 GB video starts and the pipeline looks "stuck"
        # during SHA-1 + ffprobe + transcode.
        try:
            size_mb = exif_row.path.stat().st_size / 1e6
        except OSError:
            size_mb = 0.0
        _emit(
            f"[{idx}/{total}] {trip_folder.name}/{exif_row.path.name} "
            f"({size_mb:.1f} MB)"
        )

        asset, exif = _phase(
            lambda: build_rows(exif_row.path, trip_folder, exif_row, library),
            "exif", timings,
        )
        cs_hex = asset.checksum.hex()
        inserted = _phase(
            lambda: sink.insert_asset_and_exif(asset, exif), "insert", timings,
        )
        if inserted:
            journal.mark_done(
                cs_hex, "ingest", INGEST_VERSION,
                meta={"asset_id": asset.id},
            )
        # `we_own` says "this asset belongs to immy's pipeline." True if
        # we just inserted, OR if a prior immy run inserted it (recorded
        # in the journal). Without this, a Ctrl-C between insert-commit
        # and an enricher would leave assets stranded — the resume run
        # sees inserted=False and would skip every enricher.
        we_own = inserted or journal.is_done(cs_hex, "ingest", INGEST_VERSION)
        # If the journal recorded a prior asset_id from when we inserted
        # this row in an earlier run, prefer it — `asset.id` is currently
        # a fresh UUID from build_rows that won't match the DB row.
        if not inserted and we_own:
            prior_ingest = journal.get(cs_hex, "ingest")
            if prior_ingest and prior_ingest.get("meta", {}).get("asset_id"):
                asset.id = str(prior_ingest["meta"]["asset_id"])
                exif.asset_id = asset.id
        derivs: list[DerivativeFile] | None = None
        clip_embedded = False
        # Derivatives skip-because-already-done: rebuild DerivativeFile
        # entries from the journal so downstream phases (CLIP/faces find
        # the preview) and the marker payload are consistent with a
        # first-run output. If any staged file is missing on disk
        # (e.g. `.audit/derivatives/` was wiped), we fall through to
        # recompute.
        if (
            compute_derivatives and we_own
            and asset.asset_type in ("IMAGE", "VIDEO")
            and journal.is_done(cs_hex, "derivatives", DERIV_VERSION)
        ):
            cached = journal.get(cs_hex, "derivatives") or {}
            cached_files = (cached.get("meta") or {}).get("files") or []
            try:
                rebuilt = [
                    DerivativeFile(
                        kind=f["kind"],
                        staged_path=Path(f["staged_path"]),
                        relative_path=f["relative_path"],
                        is_progressive=bool(f.get("is_progressive", False)),
                        is_transparent=bool(f.get("is_transparent", False)),
                    )
                    for f in cached_files
                ]
            except (KeyError, TypeError):
                rebuilt = []
            if rebuilt and all(d.staged_path.is_file() for d in rebuilt):
                derivs = rebuilt
                _emit("    derivatives… [cached]")
        if compute_derivatives and we_own and derivs is None and asset.asset_type in ("IMAGE", "VIDEO"):
            proxy = None
            if asset.asset_type == "VIDEO":
                # Insta360 VID ↔ LRV pairs share (timestamp, serial);
                # DJI MP4 ↔ LRF pairs share a stem. Both proxies are
                # already H.264 and ~10-30× faster to decode than the
                # master, so feeding them to ffmpeg as `derivative_source`
                # is a huge win. `proxy_for` returns None for anything
                # that doesn't match its schema, so the calls are safe
                # to stack.
                proxy = (
                    insta360_mod.proxy_for(exif_row.path, proxy_index)
                    or dji_mod.proxy_for(exif_row.path, dji_proxy_index)
                )
            # De-warp only applies to the master file. When we have a
            # proxy (LRV), it's already the camera's stitched
            # equirectangular preview — feeding it through v360 would
            # double-warp. No proxy + recognised Insta360 master →
            # single-lens fisheye → flat perspective crop.
            dewarp = (
                insta360_mod.dewarp_vf(exif_row.path)
                if asset.asset_type == "VIDEO" and proxy is None else None
            )
            # Videos hit the expensive path here: full ffmpeg H.264
            # transcode of the encoded_video derivative. Images just do
            # pyvips thumb+preview, much cheaper.
            mirror = proxy_deriv_cache.get(proxy) if proxy else None
            if asset.asset_type == "IMAGE":
                label = "thumb+preview"
            elif mirror:
                label = "mirror from sibling"
            elif proxy:
                label = f"transcode via {proxy.suffix.lstrip('.').upper()} proxy"
            elif dewarp:
                label = "transcode + fisheye de-warp"
            else:
                label = "transcode"
            _emit(f"    derivatives… ({label})")
            try:
                result = _phase(
                    lambda: derivatives_mod.compute_for_asset(
                        source_media=exif_row.path,
                        asset_id=asset.id,
                        owner_id=library.owner_id,
                        asset_type=asset.asset_type,
                        trip_folder=trip_folder,
                        transcode_videos=transcode_videos,
                        derivative_source=proxy,
                        preproc_vf=dewarp,
                        mirror_from=mirror,
                    ),
                    "derivatives", timings,
                )
                if proxy is not None and mirror is None:
                    proxy_deriv_cache[proxy] = result
                derivs = result.files
                if result.width is not None and result.height is not None:
                    sink.update_asset_dims(
                        asset.id, result.width, result.height,
                    )
                    asset.width, asset.height = result.width, result.height
                if result.duration is not None and result.duration != asset.duration:
                    sink.update_asset_duration(asset.id, result.duration)
                    asset.duration = result.duration
                if derivs is not None:
                    deriv_payload = [
                        {
                            "kind": d.kind,
                            "relative_path": d.relative_path,
                            "staged_path": str(d.staged_path),
                            "is_progressive": d.is_progressive,
                            "is_transparent": d.is_transparent,
                        }
                        for d in derivs
                    ]
                    sink.record_derivatives(asset.id, deriv_payload)
                    journal.mark_done(
                        cs_hex, "derivatives", DERIV_VERSION,
                        meta={"files": deriv_payload},
                    )
            except Exception:
                if on_derivative_error == "raise":
                    raise
                derivs = None
        # CLIP: skip if journal says done at the current model version.
        if (
            compute_clip and we_own and asset.asset_type == "IMAGE"
            and derivs is not None
            and journal.is_done(cs_hex, "clip", CLIP_VERSION)
        ):
            clip_embedded = True
            _emit(f"    CLIP embedding… [cached, {clip_model}]")
        if (
            compute_clip and we_own and asset.asset_type == "IMAGE"
            and derivs is not None and not clip_embedded
        ):
            preview = next(
                (d.staged_path for d in derivs if d.kind == "preview"), None,
            )
            if preview is not None and preview.is_file():
                _emit("    CLIP embedding…")
                try:
                    def _do_clip() -> None:
                        nonlocal clip_embedded
                        embedding = clip_mod.embed_image(preview, clip_model)
                        if expected_dim is not None and len(embedding) != expected_dim:
                            raise RuntimeError(
                                f"CLIP dim mismatch: model {clip_model!r} produced "
                                f"{len(embedding)}, smart_search expects {expected_dim}"
                            )
                        sink.upsert_clip(
                            asset.id, list(embedding),
                            clip_mod.to_pgvector_literal(embedding),
                        )
                        clip_embedded = True
                    _phase(_do_clip, "clip", timings)
                    if clip_embedded:
                        journal.mark_done(cs_hex, "clip", CLIP_VERSION)
                except Exception:
                    if on_clip_error == "raise":
                        raise
                    clip_embedded = False
        faces_detected = 0
        # Faces: journal-skip path. We don't store the face count in
        # journal meta because asset_face is the truth; on cached skip
        # we report 0 ran-this-pass, which is accurate.
        if (
            compute_faces and we_own and asset.asset_type == "IMAGE"
            and derivs is not None
            and journal.is_done(cs_hex, "faces", FACES_VERSION)
        ):
            cached_meta = (journal.get(cs_hex, "faces") or {}).get("meta") or {}
            faces_detected = int(cached_meta.get("count", 0))
            _emit(f"    faces… [cached, {faces_model}]")
        elif (
            compute_faces and we_own and asset.asset_type == "IMAGE"
            and derivs is not None
        ):
            preview = next(
                (d.staged_path for d in derivs if d.kind == "preview"), None,
            )
            if preview is not None and preview.is_file():
                _emit("    faces…")
                try:
                    faces_detected = _phase(
                        lambda: _process_faces(
                            sink, asset.id, preview, faces_model,
                        ),
                        "faces", timings,
                    )
                    journal.mark_done(
                        cs_hex, "faces", FACES_VERSION,
                        meta={"count": faces_detected},
                    )
                except Exception:
                    if on_faces_error == "raise":
                        raise
                    faces_detected = 0
        transcript_info: dict | None = None
        # Transcripts run regardless of `inserted` — they're idempotent via
        # the on-disk `<stem>.<lang>.srt` sidecar, so a second `immy process
        # --with-transcripts` pass over an already-ingested trip can
        # retro-fill transcripts without re-running the other ML workers.
        if (
            compute_transcripts and asset.asset_type == "VIDEO"
            and journal.is_done(cs_hex, "transcript", TRANSCRIPT_VERSION)
        ):
            cached_meta = (journal.get(cs_hex, "transcript") or {}).get("meta")
            transcript_info = cached_meta or None
            _emit(f"    transcript… [cached, {transcript_model}]")
        elif compute_transcripts and asset.asset_type == "VIDEO":
            make = _str(exif_row.get("EXIF:Make", "QuickTime:Make"))
            _emit("    transcript… (ffprobe → volumedetect → whisper if audio)")
            try:
                transcript_info = _phase(
                    lambda: _process_transcript(
                        sink, asset.id, exif_row.path, transcript_model,
                        make=make, prompt=transcript_prompt,
                    ),
                    "transcript", timings,
                )
                if transcript_info and "skipped" not in transcript_info:
                    sink.record_transcript(asset.id, transcript_info)
                    journal.mark_done(
                        cs_hex, "transcript", TRANSCRIPT_VERSION,
                        meta=transcript_info,
                    )
            except Exception:
                if on_transcript_error == "raise":
                    raise
                transcript_info = None
        caption_info: dict | None = None
        # Caption journal-skip path — strongest signal, used in addition
        # to (not instead of) the offline-sink prior_caption check and
        # the DB AI-prefix shortcut. `--recaption` ignores the journal,
        # forcing re-run.
        if (
            compute_captions and asset.asset_type == "IMAGE"
            and not recaption
            and journal.is_done(cs_hex, "caption", CAPTION_VERSION)
        ):
            cached_meta = (journal.get(cs_hex, "caption") or {}).get("meta")
            if cached_meta:
                caption_info = dict(cached_meta)
                caption_info.setdefault("cached", True)
                _emit(f"    caption… [cached, {CAPTION_VERSION}]")
        # Per-file resumability: if the sink has a caption recorded for
        # this asset under the same model id, skip the VLM call entirely.
        # Online path: sink.caption_info always returns None so behavior
        # is unchanged (DB description + AI-prefix guard still apply).
        # Offline path: re-running `immy process --offline` skips images
        # whose YAML already carries `caption.model == current_model` —
        # this is how a Ctrl-C'd overnight Gemma run resumes in place
        # instead of re-captioning thousands of images at 9.5 s each.
        prior_caption = (
            sink.caption_info(asset.id)
            if compute_captions and asset.asset_type == "IMAGE"
            else None
        )
        if (
            prior_caption
            and captioner_config is not None
            and prior_caption.get("model") == captioner_config.model
        ):
            caption_info = prior_caption
            _emit(f"    caption… [cached, {captioner_config.model}]")
        elif (
            compute_captions
            and asset.asset_type == "IMAGE"
            and prior_caption is None
            and not recaption
            and captioner_config is not None
        ):
            # Online resume path (and offline-without-prior): if the DB
            # description is already AI-prefixed, skip the VLM call. We
            # don't know the model it was produced with (that's not
            # stored in asset_exif), so this is a weaker signal than
            # offline's prior_caption — `--recaption` is the explicit
            # override when you do want to regenerate. Saves ~9.5 s /
            # image on Gemma; the dominant cost of a resumed run.
            existing = sink.get_description(asset.id)
            if existing and captions_mod.is_ai_description(existing):
                caption_info = {
                    "text": existing[len(captions_mod.AI_PREFIX):],
                    "model": captioner_config.model,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached": True,
                }
                _emit("    caption… [cached, DB AI-prefix]")

        if (
            compute_captions
            and caption_info is None
            and captioner_config is not None
            and asset.asset_type == "IMAGE"
        ):
            preview = None
            if derivs:
                preview = next(
                    (d.staged_path for d in derivs if d.kind == "preview"),
                    None,
                )
            _emit(f"    caption… (VLM @ {captioner_config.model})")
            try:
                caption_info = _phase(
                    lambda: _process_caption(
                        sink, asset.id, exif_row.path,
                        captioner_config, preview=preview,
                        recaption=recaption,
                    ),
                    "caption", timings,
                )
                if caption_info:
                    sink.record_caption(asset.id, caption_info)
                    journal.mark_done(
                        cs_hex, "caption", CAPTION_VERSION,
                        meta=caption_info,
                    )
            except Exception:
                if on_caption_error == "raise":
                    raise
                caption_info = None
        results.append(ProcessResult(
            asset_id=asset.id,
            container_path=asset.original_path,
            inserted=inserted,
            asset_type=asset.asset_type,
            derivatives=derivs,
            clip_embedded=clip_embedded,
            faces_detected=faces_detected,
            transcript=(
                transcript_info if transcript_info and "skipped" not in transcript_info
                else None
            ),
            caption=caption_info,
        ))

        # One-liner summary per asset: phase timings + what actually
        # ran. Kept terse so long trips don't bury the log; `immy audit`
        # / `.audit/process.yml` are the places to go deep.
        parts: list[str] = []
        if not inserted:
            parts.append("existed")
        for label in ("derivatives", "clip", "faces", "transcript", "caption"):
            if label in timings:
                parts.append(f"{label} {timings[label]:.1f}s")
        if faces_detected:
            parts.append(f"{faces_detected} face(s)")
        if transcript_info:
            if "skipped" in transcript_info:
                parts.append(f"srt:skip[{transcript_info['skipped']}]")
            else:
                parts.append(f"srt:{transcript_info.get('language', '?')}")
        if caption_info and caption_info.get("text"):
            snippet = caption_info["text"][:60].replace("\n", " ")
            parts.append(f'caption: "{snippet}…"')
        total_s = time.monotonic() - asset_t0
        _emit(f"    → {' | '.join(parts) if parts else 'nothing to do'}  ({total_s:.1f}s)")

        # Per-asset durability boundary. Without this, a Ctrl-C between
        # asset insert and end-of-trip rolls back the whole trip — the
        # next run pays for every enricher again. With per-asset commit,
        # a re-run skips committed assets via journal lookups and
        # resumes precisely at the unfinished one. Tests that want the
        # legacy "single trip transaction" semantics pass
        # commit_per_asset=False.
        if commit_per_asset:
            try:
                sink.commit()
            except Exception:
                # If the per-asset commit fails, surface it — the asset's
                # work is lost and the journal entries we just wrote
                # won't match DB state. Better to fail loud than to keep
                # accumulating ghost journal entries for non-committed
                # rows.
                journal.flush()
                raise
        journal.flush()
    return results


def _process_transcript(
    sink: Sink,
    asset_id: str,
    media: Path,
    model: str,
    *,
    make: str | None = None,
    prompt: str | None = None,
) -> dict | None:
    """Transcribe a video and write the excerpt into `asset_exif.description`.

    Three cheap guards run before Whisper is ever loaded — in ascending
    cost order so the fastest rejection wins:

    1. Sidecar already on disk → reuse. Idempotent re-runs pay nothing.
    2. EXIF make on the transcript denylist (DJI, Insta360) → skip. Zero
       I/O; EXIF is already read by the caller.
    3. No audio stream at all (ffprobe, ~100 ms) → skip.
    4. Audio stream present but mean volume below threshold (ffmpeg
       volumedetect, ~2 s on a 5-sec sample window) → skip. Catches
       GoPro/phone clips of wind noise or muted ambient.
    5. Full-file silencedetect sweep (decode-only ffmpeg pass) → skip
       when total non-silent duration is below the speech threshold.
       Catches the long-clip case the 5 s window misses, where a
       sample landed on a noise patch but the rest of the file is dead.

    Only after all five pass do we pay the ~1–5× realtime Whisper cost.
    """
    for sib in media.parent.glob(f"{media.stem}.*.srt"):
        if sib.is_file():
            # Backfill the DB description from the sidecar when it's still
            # empty — catches the case where an earlier pass wrote the
            # .srt but didn't reach the DB update (e.g. ad-hoc calls to
            # `transcripts.transcribe`). Never clobbers an existing
            # description; user-typed text wins.
            try:
                plain = transcripts_mod.srt_to_plaintext(
                    sib.read_text(encoding="utf-8", errors="replace"),
                )
                if plain:
                    excerpt = transcripts_mod.excerpt_text(plain)
                    sink.update_description_if_empty(asset_id, excerpt)
            except OSError:
                pass
            return {"path": str(sib), "language": sib.suffixes[-2].lstrip(".")}
    # Each gate returns a `skipped:<reason>` marker rather than bare None,
    # so the per-asset summary in `process_assets` can show *why* the
    # transcript phase ended in 0.1 s (denylist? silent? no audio?). The
    # caller treats any dict with a "skipped" key as "don't journal, don't
    # record" — the marker is purely for logging.
    if transcripts_mod.is_denylisted_make(make):
        return {"skipped": "denylisted-make"}
    if not transcripts_mod.has_audio(media):
        return {"skipped": "no-audio"}
    if transcripts_mod.is_silent(media):
        return {"skipped": "silent-sample"}
    # Full-file sweep: a 60-min clip with only 1–2 s of throat-clearing
    # is what currently produces `srt:fo` / `srt:nn` garbage — Whisper
    # latches onto a low-resource language because there's nothing real
    # to detect. Cheap (decode-only) compared to the Whisper pass that
    # would otherwise run.
    speech_s = transcripts_mod.speech_seconds(media)
    if speech_s is not None and speech_s < transcripts_mod.SPEECH_MIN_SECONDS:
        return {"skipped": f"silent-sweep ({speech_s:.1f}s speech)"}
    result = transcripts_mod.transcribe(media, model=model, prompt=prompt)
    if result is None:
        return {"skipped": "whisper-empty"}
    if isinstance(result, transcripts_mod.HallucinationOnly):
        # Whisper produced text but every segment was known boilerplate
        # (DimaTorzok credits, "Продолжение следует", etc.) — treat as a
        # silent clip for journaling purposes so the asset isn't queued
        # again on the next pass.
        return {"skipped": "whisper-hallucination"}
    if result.excerpt:
        # Transcripts use the empty-guard (not the AI-guard) so a user
        # description never gets clobbered, but we also don't want to
        # overwrite a prior AI caption with a transcript excerpt.
        sink.update_description_if_empty(asset_id, result.excerpt)
    return {"path": str(result.srt_path), "language": result.language}


def _process_caption(
    sink: Sink,
    asset_id: str,
    media: Path,
    config: captions_mod.CaptionerConfig,
    *,
    preview: Path | None = None,
    recaption: bool = False,
) -> dict | None:
    """Caption one image and write the prefixed description to the DB.

    Idempotence is handled at the SQL layer via `LIKE 'AI: %'`: the
    UPDATE touches only rows where the description is empty or was
    written by a previous captioner run. User-typed descriptions and
    Whisper excerpts (no prefix) are left alone.

    Two skip paths — cheapest first — avoid paying for a VLM call we'd
    either throw away or overwrite identical text with:

    1. Existing description is non-AI (user-typed, or a Whisper
       excerpt) → skip, leave it alone.
    2. Existing description is AI-prefixed → skip *unless* `recaption`
       is True. This is the online equivalent of offline's
       `prior_caption` short-circuit and is the dominant cost on a
       resumed overnight run (~9.5 s / image with Gemma). Pass
       `--recaption` to force a re-run.
    """
    existing = sink.get_description(asset_id)
    if existing and not captions_mod.is_ai_description(existing):
        return None

    result = captions_mod.caption(media, config=config, preview=preview)
    description = captions_mod.format_description(result.text)
    sink.update_description_if_ai_or_empty(asset_id, description)
    return {
        "text": result.text,
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


def _process_faces(
    sink: Sink,
    asset_id: str,
    preview_path: Path,
    model_name: str,
) -> int:
    """Detect + embed faces in the staged preview, write asset_face rows.

    Feeds the *preview* JPEG (same input CLIP sees) rather than the
    original — consistent with Immich's own pipeline, and the preview is
    small enough that Vision + ArcFace stay fast on the ANE/CPU.
    """
    image_bytes = preview_path.read_bytes()
    detected, width, height = faces_mod.detect(image_bytes)
    if not detected:
        return 0
    embedded = faces_mod.embed_faces(image_bytes, detected, model_name)
    if not embedded:
        return 0
    rows = [
        {
            "id": str(uuid.uuid4()),
            "x1": ef.face.x1, "y1": ef.face.y1,
            "x2": ef.face.x2, "y2": ef.face.y2,
            "embedding": faces_mod.to_pgvector_literal(ef.embedding),
        }
        for ef in embedded
    ]
    sink.replace_faces(asset_id, width, height, rows)
    return len(rows)


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
        if r.clip_embedded:
            entry["clip_embedded"] = True
        if r.faces_detected:
            entry["faces_detected"] = r.faces_detected
        if r.transcript:
            entry["transcript"] = r.transcript
        if r.caption:
            entry["caption"] = r.caption
        assets.append(entry)
    payload = {
        "processed_at": int(time.time()),
        "inserted": sum(1 for r in results if r.inserted),
        "already_present": sum(1 for r in results if not r.inserted),
        "derivatives_staged": sum(
            len(r.derivatives) for r in results if r.derivatives
        ),
        "clip_embedded": sum(1 for r in results if r.clip_embedded),
        "faces_detected": sum(r.faces_detected for r in results),
        "transcripts_written": sum(1 for r in results if r.transcript),
        "captions_written": sum(1 for r in results if r.caption),
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


def is_trip_fully_cached(trip_folder: Path) -> tuple[bool, int]:
    """True when `.audit/y_processed.yml` exists, the count of ingestable
    media files matches the marker, and no source file has been modified
    since the marker was written. Returns `(cached, file_count)` so the
    caller can log a skip without re-walking.

    The check is one `stat()` per file — orders of magnitude cheaper than
    `read_folder()` (which spawns exiftool over every file). Paired DJI
    `.LRF` proxies are filtered before counting because they aren't
    ingested as standalone assets, so the marker doesn't list them.

    Trusts the marker. If a file is hand-edited without bumping mtime, a
    re-run will skip it; pass `--force` (or delete the marker) to redo.
    """
    from .exif import iter_media as _iter_media
    from . import dji as _dji
    from . import raw as _raw

    marker = read_marker(trip_folder)
    if not marker:
        return False, 0
    processed_at = marker.get("processed_at")
    if not isinstance(processed_at, (int, float)):
        return False, 0
    expected = marker.get("assets") or []
    files = list(_iter_media(trip_folder))
    proxy_index = _dji.build_proxy_index(files)
    files = [f for f in files if not _dji.is_paired_proxy(f, proxy_index)]
    raw_index = _raw.build_raw_index(files)
    files = [f for f in files if not _raw.is_paired_preview(f, raw_index)]
    if len(files) != len(expected):
        return False, len(files)
    try:
        newest = max(f.stat().st_mtime for f in files)
    except (OSError, ValueError):
        return False, len(files)
    return newest <= float(processed_at), len(files)


__all__ = [
    "AssetRow", "AssetExifRow", "ProcessResult",
    "build_rows", "path_checksum", "container_path_for", "asset_type_for",
    "insert_asset", "process_trip", "write_marker", "read_marker",
    "is_processed", "is_trip_fully_cached", "marker_path", "Y_MARKER_FILENAME",
]
