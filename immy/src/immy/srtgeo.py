"""SRT → durable GPS in the Immich DB, plus the verify-channel probe.

For drone **video** assets, GPS written to an XMP sidecar never reaches
the DB: Immich's metadata extraction reads only container tags for videos
(XMP is images-only — see `offline.py`), and the asset already exists with
NULL coords, so immy's `ON CONFLICT DO NOTHING` insert can't help. The
only way to populate `asset_exif.latitude/longitude` durably is a direct
`UPDATE`, and to survive a *metadata refresh* the coords must be locked in
`asset_exif.lockedProperties` — exactly how descriptions are made durable
(`offline._LOCK_DESCRIPTION_SQL`).

`verify_channel` proves, against a live Immich, which write survives a
refresh; `geotag_folder` then applies the proven channel to a trip.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import srt
from .exif import ExifRow, has_valid_gps
from .immich import ImmichClient
from .pg import LibraryInfo


# Immich stores `lockedProperties` as a text[] of metadata-field names; the
# coordinate fields are named for their `asset_exif` columns. Overridable
# from the CLI so the probe can confirm / try alternatives.
GPS_LOCK_TOKENS: tuple[str, ...] = ("latitude", "longitude")

# Idempotent set-union append: keep whatever is locked, add our tokens, dedup.
_LOCK_GPS_FRAGMENT = """,
    "lockedProperties" = (
      SELECT array(SELECT DISTINCT unnest(
        coalesce("lockedProperties", '{}') || %(lock_tokens)s::text[]
      ))
    )"""

_READ_GPS_SQL = (
    'SELECT latitude, longitude, "lockedProperties" '
    'FROM asset_exif WHERE "assetId" = %s'
)


def write_gps(
    conn: psycopg.Connection,
    asset_id: str,
    lat: float | None,
    lon: float | None,
    *,
    lock: bool = True,
    lock_tokens: tuple[str, ...] = GPS_LOCK_TOKENS,
) -> int:
    """UPDATE `asset_exif` coords for one asset; optionally lock them.

    Caller owns the transaction — commit before triggering an Immich
    refresh so the other connection sees the write. Returns rowcount."""
    sql = "UPDATE asset_exif SET latitude = %(lat)s, longitude = %(lon)s"
    params: dict = {"lat": lat, "lon": lon, "asset_id": asset_id}
    if lock:
        sql += _LOCK_GPS_FRAGMENT
        params["lock_tokens"] = list(lock_tokens)
    sql += ' WHERE "assetId" = %(asset_id)s'
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def unlock_gps(
    conn: psycopg.Connection,
    asset_id: str,
    *,
    lock_tokens: tuple[str, ...] = GPS_LOCK_TOKENS,
) -> None:
    """Remove our coord tokens from `lockedProperties` (probe cleanup)."""
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE asset_exif SET "lockedProperties" = ('
            "  SELECT array(SELECT unnest(coalesce(\"lockedProperties\", '{}')) "
            "  EXCEPT SELECT unnest(%(lock_tokens)s::text[]))"
            ') WHERE "assetId" = %(asset_id)s',
            {"asset_id": asset_id, "lock_tokens": list(lock_tokens)},
        )


def read_gps(
    conn: psycopg.Connection, asset_id: str,
) -> tuple[float | None, float | None, list[str]]:
    """Return (latitude, longitude, lockedProperties) for one asset."""
    row = conn.execute(_READ_GPS_SQL, (asset_id,)).fetchone()
    if row is None:
        return None, None, []
    lat, lon, locked = row
    return (
        float(lat) if lat is not None else None,
        float(lon) if lon is not None else None,
        list(locked or []),
    )


# --- verify-channel probe -------------------------------------------------

@dataclass
class ChannelResult:
    channel: str
    wrote: tuple[float, float]
    survived: bool
    final: tuple[float | None, float | None]
    locked_after: list[str]
    note: str = ""


def _wait_for_refresh(
    conn: psycopg.Connection,
    asset_id: str,
    wrote: tuple[float, float],
    *,
    budget_s: float,
    poll_s: float = 2.0,
) -> tuple[float | None, float | None]:
    """Poll until the coords change away from what we wrote, or budget runs
    out. A change to NULL means the refresh clobbered the field. No change
    by the deadline means it survived. Each poll needs a fresh read, so
    commit first to drop any open snapshot."""
    deadline = time.monotonic() + budget_s
    last = wrote
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        conn.commit()  # end the txn so the next SELECT sees other writers
        lat, lon, _ = read_gps(conn, asset_id)
        last = (lat, lon)
        if lat is None or lon is None or (lat, lon) != wrote:
            break
    return last


def verify_channel(
    conn: psycopg.Connection,
    client: ImmichClient,
    asset_id: str,
    *,
    sentinel: tuple[float, float] = (12.345678, 98.765432),
    refresh_wait_s: float = 40.0,
    lock_tokens: tuple[str, ...] = GPS_LOCK_TOKENS,
) -> list[ChannelResult]:
    """Probe two DB write channels against a live Immich, restoring the
    asset's original coords at the end.

    Channel A (update_only): UPDATE coords, no lock → does a metadata
    refresh clobber them? Channel B (update_locked): UPDATE + lockedProperties
    → do the lock tokens make them durable? The decisive answer is whether B
    survives (it should) and whether A does (it may not)."""
    orig_lat, orig_lon, orig_locked = read_gps(conn, asset_id)
    results: list[ChannelResult] = []

    for channel, lock in (("update_only", False), ("update_locked", True)):
        lat, lon = sentinel
        write_gps(conn, asset_id, lat, lon, lock=lock, lock_tokens=lock_tokens)
        conn.commit()
        client.refresh_metadata([asset_id])
        final = _wait_for_refresh(
            conn, asset_id, sentinel, budget_s=refresh_wait_s)
        _, _, locked_after = read_gps(conn, asset_id)
        survived = final == sentinel
        results.append(ChannelResult(
            channel=channel, wrote=sentinel, survived=survived,
            final=final, locked_after=locked_after,
            note="" if survived else "coords were overwritten by refresh",
        ))
        # Reset before the next channel so they don't contaminate each other.
        unlock_gps(conn, asset_id, lock_tokens=lock_tokens)
        write_gps(conn, asset_id, None, None, lock=False)
        conn.commit()

    # Restore original state.
    write_gps(conn, asset_id, orig_lat, orig_lon, lock=False)
    if orig_locked:
        # Re-apply only the tokens that were there before we touched it.
        keep = tuple(t for t in orig_locked)
        write_gps(conn, asset_id, orig_lat, orig_lon, lock=True, lock_tokens=keep)
    conn.commit()
    return results


# --- geotag a trip --------------------------------------------------------

@dataclass
class GeotagOutcome:
    media: Path
    asset_id: str | None
    lat: float | None = None
    lon: float | None = None
    status: str = ""  # "tagged" | "skip-has-gps" | "no-srt" | "no-fix"
                      # | "no-asset" | "would-tag"


def _resolve_asset_id(
    conn: psycopg.Connection, library: LibraryInfo, container_path: str,
) -> str | None:
    from . import process as process_mod
    cs = process_mod.path_checksum(container_path)
    row = conn.execute(
        'SELECT id FROM asset WHERE "ownerId" = %s AND "libraryId" = %s '
        "AND checksum = %s",
        (library.owner_id, library.id, cs),
    ).fetchone()
    return str(row[0]) if row else None


def geotag_folder(
    conn: psycopg.Connection,
    library: LibraryInfo,
    trip_folder: Path,
    rows: list[ExifRow],
    *,
    write: bool,
    lock_tokens: tuple[str, ...] = GPS_LOCK_TOKENS,
    emit=lambda _msg: None,
) -> list[GeotagOutcome]:
    """For every media row lacking a usable GPS fix that has a sibling DJI
    `.SRT`, write the first valid fix (takeoff point) to the Immich DB via
    the durable channel. Idempotent: assets already carrying coords in the
    DB are skipped. `write=False` is a dry run."""
    from . import process as process_mod
    outcomes: list[GeotagOutcome] = []
    for row in rows:
        if has_valid_gps(row):
            outcomes.append(GeotagOutcome(row.path, None, status="skip-has-gps"))
            continue
        srt_path = srt.find_sibling(row.path)
        if srt_path is None:
            continue  # not a drone clip; silent
        fix = srt.first_valid_fix(srt.parse_track(srt_path))
        if fix is None:
            outcomes.append(GeotagOutcome(row.path, None, status="no-fix"))
            continue
        cpath = process_mod.container_path_for(
            row.path, trip_folder, library.container_root)
        asset_id = _resolve_asset_id(conn, library, cpath)
        if asset_id is None:
            outcomes.append(GeotagOutcome(
                row.path, None, status="no-asset",
            ))
            emit(f"  [no-asset] {row.path.name} — not in Immich library yet")
            continue
        # DB-presence idempotency: don't overwrite coords already there.
        db_lat, db_lon, _ = read_gps(conn, asset_id)
        if db_lat is not None and db_lon is not None:
            outcomes.append(GeotagOutcome(
                row.path, asset_id, db_lat, db_lon, status="skip-has-gps"))
            continue
        if not write:
            outcomes.append(GeotagOutcome(
                row.path, asset_id, fix.latitude, fix.longitude,
                status="would-tag"))
            emit(f"  [would-tag] {row.path.name} → "
                 f"({fix.latitude:.6f}, {fix.longitude:.6f})")
            continue
        write_gps(conn, asset_id, fix.latitude, fix.longitude,
                  lock=True, lock_tokens=lock_tokens)
        outcomes.append(GeotagOutcome(
            row.path, asset_id, fix.latitude, fix.longitude, status="tagged"))
        emit(f"  [tagged] {row.path.name} → "
             f"({fix.latitude:.6f}, {fix.longitude:.6f})")
    return outcomes


# --- caption context ------------------------------------------------------

# Reverse-geocode cache, kept separate from geocode_place's forward
# name→coords cache (different key direction). Best-effort + offline-safe.
_REV_CACHE_PATH = Path.home() / ".immy" / "places_reverse.yml"
_NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "immy/0.1 (https://github.com/faeton/immich-my)"


def _notes_place(trip_folder: Path) -> str | None:
    """The curated `location.name` from the trip's notes front-matter."""
    from .notes import parse_frontmatter, resolve as resolve_notes
    notes = resolve_notes(trip_folder)
    if notes is None:
        return None
    loc = parse_frontmatter(notes).get("location") or {}
    if isinstance(loc, dict):
        name = loc.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Coords → a short place string via Nominatim, cached. Silent on any
    network/parse failure so an offline run never errors."""
    import json as _json
    import urllib.parse
    import urllib.request

    import yaml

    key = f"{lat:.4f},{lon:.4f}"
    cache: dict = {}
    if _REV_CACHE_PATH.is_file():
        try:
            cache = yaml.safe_load(_REV_CACHE_PATH.read_text()) or {}
        except yaml.YAMLError:
            cache = {}
    if isinstance(cache.get(key), str):
        return cache[key]
    params = urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 10})
    req = urllib.request.Request(
        f"{_NOMINATIM_REVERSE}?{params}", headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            hit = _json.loads(resp.read())
    except Exception:
        return None
    name = hit.get("display_name") if isinstance(hit, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    name = ", ".join(name.split(", ")[:3])  # keep it short
    cache[key] = name
    try:
        _REV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REV_CACHE_PATH.write_text(yaml.safe_dump(cache, sort_keys=True))
    except OSError:
        pass
    return name


def caption_context_for(
    media: Path, trip_folder: Path, *, reverse: bool = True,
) -> str | None:
    """Build a grounding hint for the VLM from a drone clip's `.SRT`:
    altitude above ground + place. Returns None for non-drone media (no
    sibling `.SRT`) so the caption request stays byte-identical there.

    Place source, cheapest first: curated notes `location.name`, then (when
    `reverse`) a cached Nominatim reverse-geocode of the takeoff coord."""
    srt_path = srt.find_sibling(media)
    if srt_path is None:
        return None
    fix = srt.first_valid_fix(srt.parse_track(srt_path))
    place = _notes_place(trip_folder)
    if place is None and reverse and fix is not None and fix.has_fix():
        place = _reverse_geocode(fix.latitude, fix.longitude)

    parts = ["aerial drone shot"]
    if fix is not None and fix.rel_alt is not None:
        parts.append(f"~{fix.rel_alt:.0f} m above ground")
    if place:
        parts.append(f"near {place}")
    # Only worth sending if we actually learned something beyond "aerial".
    if len(parts) == 1:
        return None
    return ", ".join(parts) + "."


def is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False
