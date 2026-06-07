"""Backfill capture dates for already-ingested dateless assets.

The problem this solves: `immy process` inserts EXIF with
`INSERT INTO asset_exif ... ON CONFLICT ("assetId") DO NOTHING`, so an
asset that already has an `asset_exif` row with `dateTimeOriginal = NULL`
will *never* get its date fixed by re-running ingest. DJI drone videos are
the canonical case — DJI stores the capture instant in a sibling `.SRT`
telemetry file, not in QuickTime tags, so footage promoted before immy's
`dji-date-from-srt` rule landed sits dateless in Immich and on the wrong
spot on the timeline.

This module does the explicit `UPDATE` that ingest can't:

1. read the capture wall-clock from each file's `.SRT` (or an embedded /
   filename fallback),
2. match the local file to its Immich asset by `originalPath` (robust to
   the `DJI_0001.MOV`-collides-across-cards problem that filename matching
   has),
3. update `asset_exif."dateTimeOriginal"` + `asset."localDateTime"` (the
   stored column Immich orders the timeline by) under a hard
   "only if currently dateless" guard so a real date is never clobbered.

Default is plan/report only; the CLI applies under `--apply`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .exif import ExifRow, read_folder
from .filenames import parse_date as parse_filename_date
from .pg import LibraryInfo
from .process import _best_datetime, _parse_exif_datetime, container_path_for
from .srt import find_sibling, parse as parse_srt
from .rules.trip_timezone_guess import _tz_finder, guess_timezone


_QUICKTIME_EXTS = {".mp4", ".mov", ".m4v"}


def _quicktime_create_date(media_path: Path) -> datetime | None:
    """Full (non `-fast2`) read of the container CreateDate.

    immy's `read_folder` uses exiftool `-fast2`, which skips the MP4 `moov`
    atom — so DJI's `QuickTime:CreateDate` is invisible to ingest and these
    clips land dateless even though the date is right there. Per the
    QuickTime spec (and verified against DJI footage: CreateDate matches the
    file's modification time expressed in UTC) the value is UTC, so the
    caller tags it kind="utc". One exiftool spawn per file — only reached as
    a last resort, so the cost stays bounded to genuinely SRT-less clips.
    """
    if media_path.suffix.lower() not in _QUICKTIME_EXTS:
        return None
    try:
        out = subprocess.run(
            ["exiftool", "-n", "-s3", "-CreateDate", str(media_path)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    except Exception:
        return None
    return _parse_exif_datetime(out) if out else None


# --- date resolution ------------------------------------------------------


def resolve_capture(media_path: Path, row: ExifRow) -> tuple[datetime, str, str] | None:
    """Find the capture instant for a dateless file. Backfill-specific
    authority order (SRT first — these files are dateless *because* their
    embedded tags are empty, and SRT is the camera's own record):

        SRT telemetry → embedded QuickTime/EXIF → filename pattern.

    Returns `(dt, source_label, kind)` or None, where `kind` says how to
    interpret `dt`:
      - "utc"   — `dt` is an absolute instant (DJI SRT wall-clock is UTC; a
                  tz-aware embedded tag is absolute). localDateTime is then
                  derived by converting into the trip zone.
      - "local" — `dt` is local wall-clock (filename stamp, naive embedded
                  tag). It IS the localDateTime; the absolute instant is
                  derived by interpreting it in the trip zone.

    DJI SRT carries no tz marker but the timestamp is UTC — verified against
    real footage: a Hawaii clip stamped `03:32` with bright-daylight exposure
    is 17:32 local (UTC-10), a golden-hour flight, not 3 AM.
    """
    srt = find_sibling(media_path)
    if srt is not None:
        tele = parse_srt(srt)
        if tele.datetime_original is not None:
            return tele.datetime_original, f"SRT {srt.name}", "utc"

    embedded = _best_datetime(row)
    if embedded is not None:
        kind = "utc" if embedded.tzinfo is not None else "local"
        return embedded, "embedded EXIF/QuickTime", kind

    fn = parse_filename_date(media_path)
    if fn is not None:
        return fn.dt, f"filename {media_path.name}", "local"

    # Last resort: DJI's QuickTime CreateDate, which immy's -fast2 ingest read
    # skips. UTC.
    qt = _quicktime_create_date(media_path)
    if qt is not None:
        return qt, "QuickTime CreateDate", "utc"

    return None


def _compute_instant(
    dt: datetime, kind: str, tz_name: str | None,
) -> tuple[datetime, datetime]:
    """Return `(local_date_time, date_time_original_utc)`.

    `local_date_time` is the naive wall-clock Immich sorts the timeline by;
    `date_time_original_utc` is the absolute instant for the metadata panel.

    - kind="utc": `dt` is an absolute instant. The absolute time is known;
      localDateTime is that instant rendered in the trip zone (or left at the
      UTC wall numbers if no zone is known — the caller warns).
    - kind="local": `dt` is the wall clock the user saw. That IS
      localDateTime; the absolute instant comes from interpreting it in the
      trip zone (or treating the wall numbers as UTC if no zone is known).
    """
    if kind == "utc":
        abs_utc = (
            dt.astimezone(timezone.utc) if dt.tzinfo is not None
            else dt.replace(tzinfo=timezone.utc)
        )
        if tz_name is not None:
            local = abs_utc.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
        else:
            local = abs_utc.replace(tzinfo=None)
        return local, abs_utc

    local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    if tz_name is not None:
        abs_utc = local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
    else:
        abs_utc = local.replace(tzinfo=timezone.utc)
    return local, abs_utc


# --- timezone for the trip ------------------------------------------------


def _tz_from_srt(rows: list[ExifRow], folder: Path) -> tuple[str, str] | None:
    """Best-effort trip zone from any DJI SRT's GPS, when no file carries
    EXIF GPS and notes have no coords (so `guess_timezone` returns None)."""
    finder = _tz_finder()
    for row in rows:
        srt = find_sibling(row.path)
        if srt is None:
            continue
        tele = parse_srt(srt)
        if tele.latitude is None or tele.longitude is None:
            continue
        zone = finder.timezone_at(lat=tele.latitude, lng=tele.longitude)
        if zone:
            return zone, f"SRT GPS [{tele.latitude:.4f}, {tele.longitude:.4f}]"
    return None


def resolve_timezone(
    rows: list[ExifRow], folder: Path, override: str | None,
) -> tuple[str | None, str]:
    """`(tz_name | None, reason)`. Order: explicit override → notes/EXIF-GPS
    guess → SRT-GPS guess → none (wall-as-UTC)."""
    if override:
        # Validate early so a typo fails before any DB write.
        ZoneInfo(override)
        return override, "explicit --timezone"
    guessed = guess_timezone(rows, folder)
    if guessed is not None:
        return guessed[0], guessed[1]
    from_srt = _tz_from_srt(rows, folder)
    if from_srt is not None:
        return from_srt
    return None, "no zone signal — wall clock stored as UTC numbers"


# --- planning -------------------------------------------------------------


@dataclass
class Candidate:
    media_path: Path
    asset_id: str
    original_path: str
    source: str
    tz_name: str | None
    local_date_time: datetime
    date_time_original: datetime  # tz-aware UTC
    file_size: int
    mode: str  # 'update' (exif row exists, date NULL) | 'insert' (no exif row)


@dataclass
class FolderPlan:
    folder: Path
    tz_name: str | None
    tz_reason: str
    candidates: list[Candidate] = field(default_factory=list)
    already_dated: int = 0          # matched but already has a date
    no_date_source: list[Path] = field(default_factory=list)
    unmatched: list[Path] = field(default_factory=list)


_MATCH_SQL = """
SELECT a.id, e."assetId" AS exif_assetid, e."dateTimeOriginal"
FROM asset a
LEFT JOIN asset_exif e ON e."assetId" = a.id
WHERE a."originalPath" = %(path)s
  AND (a."libraryId" = %(lib)s OR a."libraryId" IS NULL)
  AND a."deletedAt" IS NULL
"""


def plan_folder(
    conn,
    library: LibraryInfo,
    folder: Path,
    *,
    tz_override: str | None = None,
) -> FolderPlan:
    """Match every dateless local media file in `folder` to its Immich asset
    and compute the date/zone we'd write. No DB writes."""
    rows = read_folder(folder)
    tz_name, tz_reason = resolve_timezone(rows, folder, tz_override)
    plan = FolderPlan(folder=folder, tz_name=tz_name, tz_reason=tz_reason)

    for row in rows:
        media = row.path
        resolved = resolve_capture(media, row)
        if resolved is None:
            plan.no_date_source.append(media)
            continue
        dt, source, kind = resolved
        original_path = container_path_for(media, folder, library.container_root)

        with conn.cursor() as cur:
            cur.execute(_MATCH_SQL, {"path": original_path, "lib": library.id})
            match = cur.fetchone()
        if match is None:
            plan.unmatched.append(media)
            continue
        asset_id, exif_assetid, existing_dto = match
        if existing_dto is not None:
            plan.already_dated += 1
            continue

        ldt, dto = _compute_instant(dt, kind, tz_name)
        try:
            size = media.stat().st_size
        except OSError:
            size = 0
        plan.candidates.append(Candidate(
            media_path=media,
            asset_id=str(asset_id),
            original_path=original_path,
            source=source,
            tz_name=tz_name,
            local_date_time=ldt,
            date_time_original=dto,
            file_size=size,
            mode="update" if exif_assetid is not None else "insert",
        ))

    return plan


# --- apply ----------------------------------------------------------------


_UPDATE_EXIF = """
UPDATE asset_exif
SET "dateTimeOriginal" = %(dto)s,
    "timeZone" = COALESCE("timeZone", %(tz)s)
WHERE "assetId" = %(aid)s AND "dateTimeOriginal" IS NULL
"""

_INSERT_EXIF_MIN = """
INSERT INTO asset_exif ("assetId", "dateTimeOriginal", "timeZone", "fileSizeInByte")
VALUES (%(aid)s, %(dto)s, %(tz)s, %(size)s)
ON CONFLICT ("assetId") DO NOTHING
"""

_UPDATE_ASSET = """
UPDATE asset
SET "localDateTime" = %(ldt)s,
    "fileCreatedAt" = %(dto)s
WHERE id = %(aid)s
"""


def apply_plan(conn, plan: FolderPlan) -> int:
    """Write a folder's candidates in one transaction. The exif write keeps
    its `dateTimeOriginal IS NULL` / `ON CONFLICT DO NOTHING` guard, so a row
    that got a date concurrently is left untouched and its asset row is not
    re-dated either (we only touch the asset when the exif write hit). Returns
    the number of assets actually dated."""
    written = 0
    try:
        with conn.cursor() as cur:
            for c in plan.candidates:
                params = {
                    "aid": c.asset_id,
                    "dto": c.date_time_original,
                    "tz": c.tz_name,
                    "ldt": c.local_date_time,
                    "size": c.file_size,
                }
                if c.mode == "update":
                    cur.execute(_UPDATE_EXIF, params)
                else:
                    cur.execute(_INSERT_EXIF_MIN, params)
                if cur.rowcount != 1:
                    # Already dated concurrently / lost the conflict — skip
                    # the asset write so we never re-date a row we didn't own.
                    continue
                cur.execute(_UPDATE_ASSET, params)
                written += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written
