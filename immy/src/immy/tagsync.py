"""Push notes-derived tags to Immich's native Tag API for every asset in a
trip.

`immy audit`'s `trip-tags-from-notes` rule writes the trip's `tags:` (gear/
camera, event, source) to each file's `.xmp` sidecar as HierarchicalSubject/
Subject, which Immich reads back for **photos** on its own library scan. It
never does for **videos** — same blind spot documented in `srtgeo.py` for
GPS: Immich's video metadata extraction reads only container tags, never an
external XMP sidecar. So a DJI/Insta360 clip's Gear/Camera tag, event tag,
etc. never reach Immich unless pushed through the native Tag API directly.

`tag_sync_folder` is that push: it recomputes the exact same per-file tag
set `trip-tags-from-notes` would (via `rules.trip_tags.tags_for_file`, so
the two channels never disagree), resolves each file to its Immich asset id
the same way `srtgeo.resolve_asset_id` does, and calls `upsert_tags` +
`tag_assets`. Safe to re-run: both the Immich tag API and the resolution are
idempotent.

Add-only, by design: if you remove a tag from a trip's notes and re-run,
the stale tag is NOT detached from assets that already carry it — this
mirrors the underlying Immich Tag API (`tag_assets` only attaches) and the
existing XMP rule has the same property. Detaching would need to diff
against what was pushed last time, which this module doesn't track.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg

from . import devices as devices_mod
from . import process as process_mod
from .exif import ExifRow, read_folder
from .immich import ImmichClient
from .notes import parse_frontmatter, resolve as resolve_notes
from .pg import LibraryInfo
from .rules.trip_tags import file_camera, tags_for_file
from .srtgeo import resolve_asset_id


@dataclass
class TagSyncOutcome:
    media: Path
    asset_id: str | None
    tags: tuple[str, ...] = ()
    status: str = ""  # "tagged" | "would-tag" | "no-tags" | "no-asset"
                      # | "tag-failed"


# Immich's video metadata extraction never populates asset_exif.make/model
# for DJI clips (the MP4 container carries no such tags — same blind spot
# as GPS), so the Details panel's "Camera" row is blank. Verified live
# 2026-07-12 (`srt verify-channel`-style probe): unlike GPS, a metadata
# refresh does NOT clobber an *unlocked* make/model write either — Immich
# only ever sets these fields from a fresh file read, never nulls them out
# when the file has none. Locked anyway, as a safety net matching the GPS
# precedent (and in case that behavior differs in a future Immich version).
CAMERA_LOCK_TOKENS: tuple[str, ...] = ("make", "model")

_READ_CAMERA_SQL = (
    'SELECT make, model, "lockedProperties" '
    'FROM asset_exif WHERE "assetId" = %s'
)


def read_camera(
    conn: psycopg.Connection, asset_id: str,
) -> tuple[str | None, str | None, list[str]]:
    row = conn.execute(_READ_CAMERA_SQL, (asset_id,)).fetchone()
    if row is None:
        return None, None, []
    make, model, locked = row
    return make, model, list(locked or [])


def write_camera(
    conn: psycopg.Connection,
    asset_id: str,
    make: str | None,
    model: str | None,
    *,
    lock_tokens: tuple[str, ...] = CAMERA_LOCK_TOKENS,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE asset_exif SET make = %(make)s, model = %(model)s, '
            '"lockedProperties" = (SELECT array(SELECT DISTINCT unnest('
            "  coalesce(\"lockedProperties\", '{}') || %(lock_tokens)s::text[]"
            '))) WHERE "assetId" = %(asset_id)s',
            {
                "make": make, "model": model,
                "lock_tokens": list(lock_tokens), "asset_id": asset_id,
            },
        )
        return cur.rowcount


@dataclass
class CameraSyncOutcome:
    media: Path
    asset_id: str | None
    make: str | None = None
    model: str | None = None
    status: str = ""  # "written" | "corrected" | "would-write"
                      # | "would-correct" | "skip-has-camera" | "no-signal"
                      # | "no-asset"


def trip_tags(trip_folder: Path) -> list[str]:
    """The trip's `tags:` list from its notes front-matter, or `[]`."""
    notes = resolve_notes(trip_folder)
    if notes is None:
        return []
    tags = parse_frontmatter(notes).get("tags") or []
    return [t for t in tags if isinstance(t, str) and t.strip()]


def tag_sync_folder(
    conn: psycopg.Connection,
    client: ImmichClient,
    library: LibraryInfo,
    trip_folder: Path,
    rows: list[ExifRow] | None = None,
    *,
    write: bool,
    emit=lambda _msg: None,
) -> list[TagSyncOutcome]:
    """For every media file in `trip_folder`, resolve its notes-derived tag
    set and push it to Immich's native Tag API. `write=False` is a dry run
    (no API calls)."""
    tags = trip_tags(trip_folder)
    if not tags:
        return []
    if rows is None:
        rows = read_folder(trip_folder)

    outcomes: list[TagSyncOutcome] = []
    by_tag: dict[str, list[str]] = {}
    # (row, asset_id, per_file) for files with tags + a resolved asset — status
    # is only known once the actual API push (or lack of one) has run, so
    # these are finalized into `outcomes` afterward rather than eagerly.
    pending: list[tuple[ExifRow, str, list[str]]] = []
    for row in rows:
        cam = file_camera(row)
        per_file = tags_for_file(cam, tags)
        if not per_file:
            outcomes.append(TagSyncOutcome(row.path, None, status="no-tags"))
            continue
        cpath = process_mod.container_path_for(
            row.path, trip_folder, library.container_root)
        asset_id = resolve_asset_id(conn, library, cpath)
        if asset_id is None:
            outcomes.append(TagSyncOutcome(row.path, None, status="no-asset"))
            emit(f"  [no-asset] {row.path.name} — not in Immich library yet")
            continue
        for t in per_file:
            by_tag.setdefault(t, []).append(asset_id)
        pending.append((row, asset_id, per_file))

    # Which requested tag names Immich actually gave us an id for — a name
    # missing here means `upsert_tags` didn't echo it back (e.g. a response
    # shape mismatch), so the follow-up `tag_assets` call for it never fires.
    # See `upsert_tags`'s docstring for the concrete bug this guards against:
    # a full-library run once reported success here while attaching nothing.
    resolved_ids: dict[str, str] = {}
    # (asset_id, tag_name) pairs `tag_assets` itself reported as failed —
    # `success=False, error="duplicate"` is expected/idempotent (already
    # attached), anything else is a genuine attach failure.
    failed_pairs: set[tuple[str, str]] = set()
    if write and by_tag:
        resolved_ids = client.upsert_tags(list(by_tag.keys()))
        failed_names = [n for n in by_tag if n not in resolved_ids]
        if failed_names:
            emit(f"  [warn] upsert_tags returned no id for: "
                 f"{', '.join(failed_names)} — assets needing only these "
                 f"tags will show as tag-failed")
        for name, asset_ids in by_tag.items():
            tid = resolved_ids.get(name)
            if tid is None:
                continue
            for r in client.tag_assets(tid, asset_ids):
                if not isinstance(r, dict) or r.get("success") is not False:
                    continue
                if r.get("error") == "duplicate":
                    continue
                aid = r.get("id")
                if aid:
                    failed_pairs.add((aid, name))
                    emit(f"  [warn] tag_assets failed for {aid} / {name}: "
                         f"{r.get('error')}")
                else:
                    # Can't tell which asset this result belongs to — fail
                    # the whole batch for this tag rather than silently
                    # treating unattributable failures as success.
                    failed_pairs.update((a, name) for a in asset_ids)
                    emit(f"  [warn] tag_assets failure with no asset id for "
                         f"{name}: {r.get('error')} — failing all "
                         f"{len(asset_ids)} asset(s) requested for this tag")

    for row, asset_id, per_file in pending:
        if not write:
            status = "would-tag"
        elif not all(t in resolved_ids for t in per_file):
            status = "tag-failed"
        elif any((asset_id, t) in failed_pairs for t in per_file):
            status = "tag-failed"
        else:
            status = "tagged"
        outcomes.append(TagSyncOutcome(row.path, asset_id, tuple(per_file), status))
        emit(f"  [{status}] {row.path.name} → {', '.join(per_file)}")

    return outcomes


def camera_sync_folder(
    conn: psycopg.Connection,
    library: LibraryInfo,
    trip_folder: Path,
    rows: list[ExifRow] | None = None,
    *,
    write: bool,
    emit=lambda _msg: None,
) -> list[CameraSyncOutcome]:
    """Backfill `asset_exif.make`/`model` for files whose container carries
    neither, resolving through `devices.resolve` (the SAME owner-confirmed
    friendly-name table `immy process` uses at ingest time) so this never
    writes a raw module code like "FC8282" — always make="DJI", model="Air 3"
    (bare, no redundant "DJI" prefix — Immich concatenates make+model for
    display, so "DJI"+"DJI Air 3" would render as "DJI DJI Air 3").

    Primary signal is the file's own raw EXIF/QuickTime Make/Model or mp4
    Encoder atom (works for DJI stills, and any video with a real encoder
    atom). DJI *video* commonly carries none of those at all — confirmed
    empirically, not assumed: `EXIF:Make`/`Model` and `ItemList:Encoder`/
    `QuickTime:Encoder` are simply absent from these files. For that case
    only, falls back to the trip's curated `Gear/Camera/<code>` notes tag
    (via `rules.trip_tags.tags_for_file`) — but resolves ITS module code
    through `devices.resolve` too, rather than using it raw.

    Idempotent, and self-correcting in two ways: an asset we already locked
    ourselves gets silently re-corrected if the newly-resolved value
    differs (guards against a stale/wrong prior write of this same command,
    or the friendly-name table gaining an entry after the fact). An asset
    with an UNLOCKED existing value (Immich's own extraction) is left alone
    *unless* that existing value is itself a known-raw code our table maps
    to something different — a confident upgrade of data we already
    recognize, never a guess at data we don't (see CHANGELOG 2026-07-12 for
    why both guards exist)."""
    tags = trip_tags(trip_folder)
    if rows is None:
        rows = read_folder(trip_folder)

    outcomes: list[CameraSyncOutcome] = []
    for row in rows:
        raw_make = row.get("EXIF:Make", "QuickTime:Make", "QuickTime:AndroidMake")
        raw_model = row.get("EXIF:Model", "QuickTime:Model", "QuickTime:AndroidModel")
        raw_encoder = row.get("ItemList:Encoder", "QuickTime:Encoder")
        make, model = devices_mod.resolve(raw_make, raw_model, raw_encoder)

        if make is None and model is None and tags:
            cam = file_camera(row)
            gear_tags = [t for t in tags_for_file(cam, tags)
                         if t.startswith("Gear/Camera/")]
            if gear_tags:
                parts = gear_tags[0].removeprefix("Gear/Camera/").strip().split(
                    maxsplit=1)
                notes_make = parts[0] if parts else None
                notes_model = parts[1] if len(parts) > 1 else None
                make, model = devices_mod.resolve(notes_make, notes_model)

        if make is None and model is None:
            outcomes.append(CameraSyncOutcome(row.path, None, status="no-signal"))
            continue

        cpath = process_mod.container_path_for(
            row.path, trip_folder, library.container_root)
        asset_id = resolve_asset_id(conn, library, cpath)
        if asset_id is None:
            outcomes.append(CameraSyncOutcome(row.path, None, status="no-asset"))
            emit(f"  [no-asset] {row.path.name} — not in Immich library yet")
            continue

        db_make, db_model, locked = read_camera(conn, asset_id)
        ours = all(t in locked for t in CAMERA_LOCK_TOKENS)
        if (db_make or db_model) and not ours:
            # Not locked by us — normally hands off (Immich's own
            # extraction). Exception: if the value ALREADY THERE is itself
            # a known-raw code our confirmed table maps to something
            # different, upgrade it — a confident lookup against the same
            # owner-confirmed table, not a guess. `devices.resolve` is a
            # no-op pass-through for anything not in its table (e.g.
            # "Apple"/"iPhone 17 Pro", "Canon"/"Canon EOS R6"), so this can
            # never touch genuinely-good extracted data. Found live
            # 2026-07-12: 632 assets across the library, predating the
            # `devices.py` mapping's existence, carrying a raw DJI code
            # this way.
            upgraded = devices_mod.resolve(db_make, db_model)
            if upgraded == (db_make, db_model):
                outcomes.append(CameraSyncOutcome(
                    row.path, asset_id, db_make, db_model, status="skip-has-camera"))
                continue
            make, model = upgraded
        elif (db_make, db_model) == (make, model):
            outcomes.append(CameraSyncOutcome(
                row.path, asset_id, make, model, status="skip-has-camera"))
            continue

        verb = "correct" if ours else "write"
        if not write:
            status = f"would-{verb}"
            outcomes.append(CameraSyncOutcome(row.path, asset_id, make, model, status=status))
            emit(f"  [{status}] {row.path.name} → {make} {model or ''}".rstrip())
            continue
        write_camera(conn, asset_id, make, model)
        status = "corrected" if ours else "written"
        outcomes.append(CameraSyncOutcome(row.path, asset_id, make, model, status=status))
        emit(f"  [{status}] {row.path.name} → {make} {model or ''}".rstrip())

    return outcomes
