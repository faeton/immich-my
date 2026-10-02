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

1. read the capture time — a `.xmp` sidecar correction first, then the
   file's `.SRT`, then the embedded tags exactly as ingest reads them
   (`capture.capture_time`: offsets honoured, QuickTime CreateDate UTC
   except local-clock makes), then a filename stamp,
2. match the local file to its Immich asset by `originalPath` (robust to
   the `DJI_0001.MOV`-collides-across-cards problem that filename matching
   has),
3. update `asset_exif."dateTimeOriginal"` + `asset."localDateTime"` (the
   stored column Immich orders the timeline by) under a hard
   "only if currently dateless" guard so a real date is never clobbered.

Default is plan/report only; the CLI applies under `--apply`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .capture import SIDECAR_SOURCE, _compute_instant, _capture_sourced
from .exif import ExifRow, read_folder
from .filenames import parse_date as parse_filename_date
from .pg import LibraryInfo
from .process import container_path_for
from .srt import find_sibling, parse as parse_srt
from .rules.trip_timezone_guess import _tz_finder, guess_timezone

if TYPE_CHECKING:
    from .paths import WritablePaths


# --- date resolution ------------------------------------------------------


def resolve_capture(
    media_path: Path, row: ExifRow,
) -> tuple[datetime, str, str, str | None] | None:
    """Find the capture instant for a dateless file. Authority order:

        .xmp SIDECAR DateTimeOriginal (a deliberate correction — never undone)
        → SRT telemetry (these files are often dateless *because* their
          embedded tags are empty, and SRT is the camera's own record)
        → embedded tags, read exactly as ingest reads them
          (`capture._capture_sourced`) → filename pattern.

    (There is no separate QuickTime re-read any more: `read_folder` runs
    exiftool `-fast`, which reads the `moov` CreateDate ingest uses.)

    Returns `(dt, source_label, kind, file_zone)` or None. `file_zone` is
    the zone the file itself records (offset tag / QuickTime TimeZone), or
    None; when present it beats any inferred trip/clip zone (an explicit
    `--timezone` still wins). `kind` says how
    to interpret `dt`:
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
    captured = _capture_sourced(row)
    if captured is not None and captured[2] == SIDECAR_SOURCE:
        dt, tz_name, source = captured
        return dt, source, "utc" if dt.tzinfo is not None else "local", tz_name

    srt = find_sibling(media_path)
    if srt is not None:
        tele = parse_srt(srt)
        if tele.datetime_original is not None:
            return tele.datetime_original, f"SRT {srt.name}", "utc", None

    if captured is not None:
        dt, tz_name, source = captured
        return dt, f"embedded {source}", "utc" if dt.tzinfo is not None else "local", tz_name

    fn = parse_filename_date(media_path)
    if fn is not None:
        return fn.dt, f"filename {media_path.name}", "local", None

    return None


# --- timezone for the trip ------------------------------------------------


def _tz_from_srt(rows: list[ExifRow], folder: Path) -> tuple[str, str] | None:
    """Best-effort trip zone from DJI SRT GPS, when no file carries EXIF GPS
    and notes have no coords (so `guess_timezone` returns None).

    Majority vote, NOT first-match: a folder that spans locations (or holds a
    stray clip from another trip) must not let one outlier zone the whole
    trip. The per-clip zone still overrides this default for clips that carry
    their own GPS — this is only the fallback for clips that don't."""
    from collections import Counter

    finder = _tz_finder()
    zones: Counter[str] = Counter()
    for row in rows:
        srt = find_sibling(row.path)
        if srt is None:
            continue
        tele = parse_srt(srt)
        if tele.latitude is None or tele.longitude is None:
            continue
        zone = finder.timezone_at(lat=tele.latitude, lng=tele.longitude)
        if zone:
            zones[zone] += 1
    if not zones:
        return None
    top, n = zones.most_common(1)[0]
    return top, f"SRT GPS majority {n}/{sum(zones.values())} → {top}"


def _clip_timezone(media_path: Path) -> str | None:
    """A single clip's zone from its own sibling-SRT GPS.

    For folders that span locations — a travel day, or a stray clip from a
    different trip filed in the wrong folder — this localises each clip to
    where it was actually shot, instead of stamping one trip-wide guess on
    everything (which mis-zones the outliers by whole hours)."""
    srt = find_sibling(media_path)
    if srt is None:
        return None
    tele = parse_srt(srt)
    if tele.latitude is None or tele.longitude is None:
        return None
    return _tz_finder().timezone_at(lat=tele.latitude, lng=tele.longitude)


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
    # 'update' (exif row exists, date NULL) | 'insert' (no exif row) |
    # 'retime' (--retime: overwrite an existing date)
    mode: str


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
    retime: bool = False,
    paths: "WritablePaths | None" = None,
) -> FolderPlan:
    """Match every dateless local media file in `folder` to its Immich asset
    and compute the date/zone we'd write. No DB writes.

    `retime=True` also re-dates assets that already have a date — used to
    correct a wrong earlier write (e.g. a mixed-location folder that got one
    trip-wide zone). Without it, dated assets are left untouched.

    `paths` (`WritablePaths`) locates the `.xmp` sidecars — under
    `sidecars_root` on the NAS; unset → beside the media (Mac).
    """
    rows = read_folder(folder, paths=paths)
    tz_name, tz_reason = resolve_timezone(rows, folder, tz_override)
    plan = FolderPlan(folder=folder, tz_name=tz_name, tz_reason=tz_reason)

    for row in rows:
        media = row.path
        resolved = resolve_capture(media, row)
        if resolved is None:
            plan.no_date_source.append(media)
            continue
        dt, source, kind, file_tz = resolved
        original_path = container_path_for(media, folder, library.container_root)

        with conn.cursor() as cur:
            cur.execute(_MATCH_SQL, {"path": original_path, "lib": library.id})
            match = cur.fetchone()
        if match is None:
            plan.unmatched.append(media)
            continue
        asset_id, exif_assetid, existing_dto = match
        if existing_dto is not None and not retime:
            plan.already_dated += 1
            continue

        # --timezone is explicit user input and wins for the whole run (the
        # instant still comes from the file's own offset; only the zone it is
        # shown in changes). Otherwise the file's own recorded zone beats
        # every inferred guess, as at ingest: then the clip's own SRT-GPS
        # zone, then the trip-wide guess.
        clip_tz = tz_override or file_tz or _clip_timezone(media) or tz_name
        if existing_dto is not None:
            mode = "retime"
        elif exif_assetid is not None:
            mode = "update"
        else:
            mode = "insert"
        ldt, dto = _compute_instant(dt, kind, clip_tz)
        try:
            size = media.stat().st_size
        except OSError:
            size = 0
        plan.candidates.append(Candidate(
            media_path=media,
            asset_id=str(asset_id),
            original_path=original_path,
            source=source,
            tz_name=clip_tz,
            local_date_time=ldt,
            date_time_original=dto,
            file_size=size,
            mode=mode,
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

_RETIME_EXIF = """
UPDATE asset_exif
SET "dateTimeOriginal" = %(dto)s,
    "timeZone" = COALESCE(%(tz)s, "timeZone")
WHERE "assetId" = %(aid)s
"""

_UPDATE_ASSET = """
UPDATE asset
SET "localDateTime" = %(ldt)s,
    "fileCreatedAt" = %(dto)s
WHERE id = %(aid)s
"""


def apply_plan(conn, plan: FolderPlan) -> int:
    """Write a folder's candidates in one transaction. The 'update' exif write
    keeps its `dateTimeOriginal IS NULL` / `ON CONFLICT DO NOTHING` guard, so a
    row that got a date concurrently is left untouched and its asset row is not
    re-dated either (we only touch the asset when the exif write hit). The
    'retime' write deliberately overwrites an existing date. Returns the number
    of assets actually dated."""
    written = 0
    try:
        with conn.cursor() as cur:
            for c in plan.candidates:
                params = {
                    "aid": c.asset_id,
                    "dto": c.date_time_original,
                    "tz": c.tz_name,
                    # localDateTime is the wall clock stored as if UTC. Tag it
                    # so Postgres doesn't read a naive value in the session
                    # TimeZone (Immich's DB runs Europe/Lisbon, not UTC).
                    "ldt": c.local_date_time.replace(tzinfo=timezone.utc),
                    "size": c.file_size,
                }
                if c.mode == "update":
                    cur.execute(_UPDATE_EXIF, params)
                elif c.mode == "retime":
                    cur.execute(_RETIME_EXIF, params)
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
