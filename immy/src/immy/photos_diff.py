"""Which Apple Photos assets are not in Immich yet — read-only, no downloads.

Phase 1 of the Photos bridge (`todo/PHOTOS-BRIDGE-REVIEW.md`): before any
`osxphotos export`, find the holes. Everything here reads two SQLite files —
`Photos.sqlite` (what the Mac's Photos.app knows, including iCloud-only
originals that are not on disk under Optimize Mac Storage) and an
`immy snapshot` (what Immich holds). Nothing is fetched from iCloud, so this
is cheap enough to run every time and needs no Apple login on n5.

The output is a UUID list for `osxphotos export --uuid-from-file`.

Matching, in order — first hit wins:

1. `exact` — same original filename (case-insensitive) AND same byte size.
   The Immich mobile app and the original-preserving imports both keep
   `originalFileName` and the untouched bytes, so this covers most of it.
2. `time`  — an Immich asset whose capture instant is within
   `TIME_TOLERANCE_S` of the Photos one. Catches renamed/re-encoded copies
   (Telegram/WhatsApp saves, the occasional HEIC→JPG). A heuristic: it can
   only make us *skip* an asset, never download a wrong one, and the dedup
   cascade on n5 is the real arbiter anyway.
3. `missing` — neither.

Filename alone is deliberately NOT a match. iPhone `IMG_NNNN` counters wrap
every 10k shots and several devices share them: on the 2026-10 library a
name-only rule wrongly claimed ~3.7k recent shots were already in Immich,
because older photos with the same `IMG_8976.HEIC` name were.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

# Seconds since 2001-01-01 UTC → Unix seconds.
_CORE_DATA_EPOCH_UNIX = 978307200

# Immich's dateTimeOriginal keeps sub-second precision for iPhone shots but
# not for everything (JPEG EXIF without SubSecTime). ±1 s absorbs that.
TIME_TOLERANCE_S = 1

# ZASSET.ZVISIBILITYSTATE: 0 = visible. 2 = a burst frame that was not the
# pick — not shown in Photos, and not something to backfill into Immich.
_VISIBLE = 0


@dataclass(frozen=True)
class PhotosAsset:
    uuid: str
    filename: str
    size_bytes: int | None
    created_unix: float | None
    added_unix: float | None
    kind: str             # "photo" | "video"
    camera: str | None


@dataclass(frozen=True)
class Classified:
    asset: PhotosAsset
    status: str           # "exact" | "time" | "missing"


@dataclass
class DiffResult:
    items: list[Classified]
    skipped_hidden_bursts: int

    def by_status(self, status: str) -> list[PhotosAsset]:
        return [c.asset for c in self.items if c.status == status]

    def counts(self) -> Counter:
        return Counter(c.status for c in self.items)


def open_live_ro(db_path) -> sqlite3.Connection:
    """Read-only, but WAL-aware.

    `apple_photos.open_ro` passes `immutable=1`, which skips the -wal file.
    That is fine for years-old face tags; here the newest additions are the
    whole point, and Photos.app keeps them in the WAL until a checkpoint.
    """
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _to_unix(core_data: float | None) -> float | None:
    return None if core_data is None else core_data + _CORE_DATA_EPOCH_UNIX


def read_photos_assets(
    conn: sqlite3.Connection, added_since: datetime | None,
) -> tuple[list[PhotosAsset], int]:
    """Non-trashed assets added on/after `added_since` (date *added*, not
    capture date — an old photo AirDropped last week is a new arrival).

    Returns `(visible_assets, hidden_burst_count)`.
    """
    since_core = (
        None if added_since is None
        else added_since.timestamp() - _CORE_DATA_EPOCH_UNIX
    )
    sql = """
        SELECT a.ZUUID, aa.ZORIGINALFILENAME, aa.ZORIGINALFILESIZE,
               a.ZDATECREATED, a.ZADDEDDATE, a.ZKIND, a.ZVISIBILITYSTATE,
               x.ZCAMERAMODEL
        FROM ZASSET a
        JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK
        LEFT JOIN ZEXTENDEDATTRIBUTES x ON x.ZASSET = a.Z_PK
        WHERE a.ZTRASHEDSTATE = 0
    """
    params: tuple = ()
    if since_core is not None:
        sql += " AND a.ZADDEDDATE >= ?"
        params = (since_core,)
    out: list[PhotosAsset] = []
    hidden = 0
    for uuid, fn, size, created, added, kind, vis, camera in conn.execute(sql, params):
        if vis not in (None, _VISIBLE):
            hidden += 1
            continue
        out.append(PhotosAsset(
            uuid=uuid,
            filename=fn or "",
            size_bytes=int(size) if size is not None else None,
            created_unix=_to_unix(created),
            added_unix=_to_unix(added),
            kind="video" if kind == 1 else "photo",
            camera=camera,
        ))
    return out, hidden


class ImmichIndex:
    """In-memory lookup over a snapshot. ~220k rows → a few seconds, and it
    avoids the snapshot's case-sensitive `(filename, size)` index."""

    def __init__(self, snap: sqlite3.Connection) -> None:
        self.name_sizes: dict[str, set[int]] = defaultdict(set)
        self.instants: set[int] = set()
        for fn, size, taken in snap.execute(
            "SELECT filename, size_bytes, taken_at FROM assets"
        ):
            if size is not None:
                self.name_sizes[fn.lower()].add(int(size))
            if taken:
                try:
                    self.instants.add(round(datetime.fromisoformat(taken).timestamp()))
                except ValueError:
                    pass

    def classify(self, a: PhotosAsset) -> str:
        if a.size_bytes is not None and a.size_bytes in self.name_sizes.get(a.filename.lower(), ()):
            return "exact"
        if a.created_unix is not None:
            t = round(a.created_unix)
            if any(t + d in self.instants
                   for d in range(-TIME_TOLERANCE_S, TIME_TOLERANCE_S + 1)):
                return "time"
        return "missing"


def diff(
    photos: sqlite3.Connection,
    snap: sqlite3.Connection,
    added_since: datetime | None,
) -> DiffResult:
    assets, hidden = read_photos_assets(photos, added_since)
    index = ImmichIndex(snap)
    return DiffResult(
        items=[Classified(a, index.classify(a)) for a in assets],
        skipped_hidden_bursts=hidden,
    )


def month(unix: float | None) -> str:
    if unix is None:
        return "unknown"
    return datetime.fromtimestamp(unix, timezone.utc).strftime("%Y-%m")
