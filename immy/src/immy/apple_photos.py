"""Read-only reader for Apple Photos' `Photos.sqlite`.

Used by `immy import-apple-people` to extract years of manual face tagging
and seed matching Immich Person rows. Apple Photos is authoritative for
"who is this" — Immich's face recognition clusters the same faces but
doesn't know their names. We bridge via `(original_filename, size)` match
against a snapshot produced by `immy snapshot`.

Schema tested on: macOS 14/15 (Photos.app 10.x). Apple rewrites this
schema every couple of majors — the queries here fail loudly if a column
disappears rather than silently returning empty.

Strictly read-only. Opens the SQLite file via `mode=ro` URI. Never writes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


# Paths inside the `.photoslibrary` bundle. Apple occasionally shuffles
# these; keep them in one place so a breakage is easy to spot.
DB_RELATIVE_PATH = "database/Photos.sqlite"


@dataclass(frozen=True)
class AppleFace:
    """One detected face row joined to its asset.

    bbox is Apple's normalized (center_x, center_y, size) — size is a
    single value because Apple stores face crops as squares. Multiply by
    source_w/source_h to get pixel coords.
    """

    apple_asset_uuid: str
    # Real filename as ingested — `ZASSET.ZFILENAME` is Apple's internal
    # UUID-based copy, useless for Immich matching.
    original_filename: str
    original_size: int | None
    center_x: float
    center_y: float
    size: float
    source_width: int | None
    source_height: int | None
    quality: float | None
    manual: bool


@dataclass
class ApplePerson:
    apple_pk: int
    full_name: str
    display_name: str | None
    faces: list[AppleFace] = field(default_factory=list)


def resolve_db_path(library: Path) -> Path:
    """Given `~/Pictures/Photos Library.photoslibrary`, return the sqlite path."""
    if library.is_file() and library.suffix == ".sqlite":
        return library
    candidate = library / DB_RELATIVE_PATH
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Photos.sqlite not found at {candidate}. "
            f"Expected either a .photoslibrary bundle or a direct .sqlite path."
        )
    return candidate


def open_ro(db_path: Path) -> sqlite3.Connection:
    """Open Photos.sqlite read-only. Photos.app can hold write locks; RO is safest."""
    uri = f"file:{db_path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


# --- person resolution ----------------------------------------------------

# Apple lets users merge clusters ("This is also Mama"). The losing row
# keeps its Z_PK but points ZMERGETARGETPERSON at the winner. Face rows
# still point at the *original* person, so we must follow the chain before
# grouping. Chains are shallow in practice (1–2 hops) but we cap anyway.
_MERGE_CHAIN_MAX = 10


def _resolve_merge(conn: sqlite3.Connection, pk: int) -> int:
    seen: set[int] = set()
    current = pk
    for _ in range(_MERGE_CHAIN_MAX):
        if current in seen:
            break  # cycle; shouldn't happen but defend
        seen.add(current)
        row = conn.execute(
            "SELECT ZMERGETARGETPERSON FROM ZPERSON WHERE Z_PK = ?", (current,),
        ).fetchone()
        if row is None or row[0] is None:
            return current
        current = row[0]
    return current


def read_named_persons(
    conn: sqlite3.Connection,
    *,
    min_faces: int = 3,
    only: set[str] | None = None,
) -> list[ApplePerson]:
    """Return named persons with their face bboxes, merge-chains resolved.

    Filters:
      - `ZFULLNAME` non-empty (excludes Apple's ~25k auto-clusters).
      - Not itself a merge loser (`ZMERGETARGETPERSON IS NULL`).
      - Asset not in trash (`ZASSET.ZTRASHEDSTATE = 0`).
      - Post-filter: `len(faces) >= min_faces`.
      - Optional name filter via `only`.
    """
    persons: dict[int, ApplePerson] = {}
    for pk, full, display in conn.execute(
        "SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME FROM ZPERSON "
        "WHERE ZFULLNAME IS NOT NULL AND ZFULLNAME != '' "
        "  AND ZMERGETARGETPERSON IS NULL",
    ):
        if only is not None and full not in only:
            continue
        persons[pk] = ApplePerson(
            apple_pk=pk, full_name=full, display_name=display,
        )

    if not persons:
        return []

    # Pull every face that points (directly or via a merge chain) at one
    # of our named persons. We rely on ZADDITIONALASSETATTRIBUTES for the
    # real original filename and size — `ZASSET.ZFILENAME` is Apple's
    # internal UUID-based name, useless for Immich matching.
    face_rows = conn.execute(
        """
        SELECT df.Z_PK,
               df.ZPERSONFORFACE,
               a.ZUUID,
               aa.ZORIGINALFILENAME,
               aa.ZORIGINALFILESIZE,
               df.ZCENTERX, df.ZCENTERY, df.ZSIZE,
               df.ZSOURCEWIDTH, df.ZSOURCEHEIGHT,
               df.ZQUALITY,
               df.ZMANUAL
        FROM ZDETECTEDFACE df
        JOIN ZASSET a
          ON df.ZASSETFORFACE = a.Z_PK
         AND (a.ZTRASHEDSTATE IS NULL OR a.ZTRASHEDSTATE = 0)
        LEFT JOIN ZADDITIONALASSETATTRIBUTES aa
          ON aa.ZASSET = a.Z_PK
        WHERE df.ZPERSONFORFACE IS NOT NULL
          AND df.ZCENTERX IS NOT NULL
          AND aa.ZORIGINALFILENAME IS NOT NULL
        """,
    ).fetchall()

    merge_cache: dict[int, int] = {}
    for (_dfpk, raw_person, uuid, fname, fsize,
         cx, cy, sz, sw, sh, quality, manual) in face_rows:
        target = merge_cache.get(raw_person)
        if target is None:
            target = _resolve_merge(conn, raw_person)
            merge_cache[raw_person] = target
        person = persons.get(target)
        if person is None:
            continue  # face belonged to a non-named or filtered person
        person.faces.append(AppleFace(
            apple_asset_uuid=uuid,
            original_filename=fname,
            original_size=int(fsize) if fsize is not None else None,
            center_x=float(cx),
            center_y=float(cy),
            size=float(sz or 0.0),
            source_width=int(sw) if sw else None,
            source_height=int(sh) if sh else None,
            quality=float(quality) if quality is not None else None,
            manual=bool(manual),
        ))

    result = [p for p in persons.values() if len(p.faces) >= min_faces]
    result.sort(key=lambda p: len(p.faces), reverse=True)
    return result


# --- matching -------------------------------------------------------------


@dataclass(frozen=True)
class FaceMatch:
    """An Apple face paired with the Immich asset it belongs to."""

    face: AppleFace
    immich_asset_id: str


def match_to_snapshot(
    persons: list[ApplePerson],
    snapshot: sqlite3.Connection,
) -> dict[int, list[FaceMatch]]:
    """For each person, return the subset of faces whose Apple original
    (filename, size) matches exactly one Immich asset in the snapshot.

    Ambiguous matches (same name+size in two libraries) are dropped rather
    than guessed — caller can rerun with `--library` to narrow the
    snapshot if that becomes a real problem.
    """
    # One scan of the snapshot builds a `(filename, size) -> [asset_id]`
    # index. Earlier we queried per face — 20k faces = 20k SQLite
    # round-trips. Full-table scan + dict lookup is O(assets + faces)
    # and lands well under a second on 200k-asset libraries.
    index: dict[tuple[str, int], list[str]] = {}
    for asset_id, filename, size in snapshot.execute(
        "SELECT asset_id, filename, size_bytes FROM assets"
    ):
        index.setdefault((filename, size), []).append(asset_id)

    out: dict[int, list[FaceMatch]] = {}
    for person in persons:
        matched: list[FaceMatch] = []
        for face in person.faces:
            if face.original_size is None:
                continue
            hits = index.get((face.original_filename, face.original_size))
            if hits and len(hits) == 1:
                matched.append(FaceMatch(face=face, immich_asset_id=hits[0]))
        out[person.apple_pk] = matched
    return out
