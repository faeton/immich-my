"""Read-only reader for Apple Photos' `Photos.sqlite`.

Used by `immy import-apple-people` to extract years of manual face tagging
and seed matching Immich Person rows. Apple Photos is authoritative for
"who is this" — Immich's face recognition clusters the same faces but
doesn't know their names. We bridge via a `(size, capture instant)` match
(falling back to `(original_filename, size)`) against a snapshot produced
by `immy snapshot` — see `match_to_snapshot` for why filename alone isn't
enough.

Schema tested on: macOS 14/15 (Photos.app 10.x). Apple rewrites this
schema every couple of majors — the queries here fail loudly if a column
disappears rather than silently returning empty.

Strictly read-only. Opens the SQLite file via `mode=ro` URI. Never writes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Paths inside the `.photoslibrary` bundle. Apple occasionally shuffles
# these; keep them in one place so a breakage is easy to spot.
DB_RELATIVE_PATH = "database/Photos.sqlite"

# Apple's Core Data reference date: seconds in ZDATECREATED count from here,
# and the result is true UTC (verified against known-good pairs: a face's
# ZDATECREATED converted this way lines up with the matching Immich asset's
# taken_at to the millisecond once taken_at's own offset is applied).
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


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
    # Capture instant (true UTC), from ZASSET.ZDATECREATED. Used as a
    # second matching signal alongside size — filename alone collides
    # constantly (IMG_#### counters reset/reuse across years and phones),
    # and Immich renames the HEIC half of a Live Photo pair to avoid
    # colliding with its .MOV/.MP4 companion (`IMG_1434.HEIC` ->
    # `IMG_1434(1).HEIC`), so filename+size alone silently misses those.
    capture_utc: datetime | None
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
               a.ZDATECREATED,
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
    for (_dfpk, raw_person, uuid, fname, fsize, zdate,
         cx, cy, sz, sw, sh, quality, manual) in face_rows:
        target = merge_cache.get(raw_person)
        if target is None:
            target = _resolve_merge(conn, raw_person)
            merge_cache[raw_person] = target
        person = persons.get(target)
        if person is None:
            continue  # face belonged to a non-named or filtered person
        capture_utc = (
            _CORE_DATA_EPOCH + timedelta(seconds=zdate)
            if zdate is not None else None
        )
        person.faces.append(AppleFace(
            apple_asset_uuid=uuid,
            original_filename=fname,
            original_size=int(fsize) if fsize is not None else None,
            capture_utc=capture_utc,
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


# Two unrelated photos sharing an exact byte size *and* a capture instant
# a handful of seconds apart isn't realistic — this is a much sharper key
# than a recycled `IMG_####` filename.
_CAPTURE_TOLERANCE = timedelta(seconds=5)


def _parse_taken_at(raw: str | None) -> datetime | None:
    """Parse the snapshot's ISO `taken_at` (with UTC offset) to true UTC."""
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # naive timestamp, can't be normalised safely
    return dt.astimezone(timezone.utc)


def match_to_snapshot(
    persons: list[ApplePerson],
    snapshot: sqlite3.Connection,
) -> dict[int, list[FaceMatch]]:
    """For each person, return the subset of faces that match exactly one
    Immich asset in the snapshot.

    Two signals are tried, in order of precision:

    1. **size + capture instant** (`capture_utc` from `ZASSET.ZDATECREATED`
       vs. the asset's `taken_at`, within `_CAPTURE_TOLERANCE`). This is
       the only signal that survives Immich renaming the HEIC half of a
       Live Photo pair to dodge its video companion
       (`IMG_1434.HEIC` -> `IMG_1434(1).HEIC`) — filename+size alone
       silently drops every one of those as a false non-match.
    2. **filename + size** — the original signal, used when a face has no
       `capture_utc` (missing `ZDATECREATED`) or the date signal was
       itself ambiguous.

    Ambiguous matches (more than one candidate under the signal in play)
    are dropped rather than guessed — caller can rerun with `--library` to
    narrow the snapshot if that becomes a real problem.
    """
    # One scan of the snapshot builds both indexes. Earlier we queried per
    # face — 20k faces = 20k SQLite round-trips. Full-table scan + dict
    # lookup is O(assets + faces) and lands well under a second on
    # 200k-asset libraries.
    namesize_index: dict[tuple[str, int], list[str]] = {}
    size_index: dict[int, list[tuple[str, datetime]]] = {}
    for asset_id, filename, size, taken_at in snapshot.execute(
        "SELECT asset_id, filename, size_bytes, taken_at FROM assets"
    ):
        if size is None:
            continue
        namesize_index.setdefault((filename, size), []).append(asset_id)
        taken_utc = _parse_taken_at(taken_at)
        if taken_utc is not None:
            size_index.setdefault(size, []).append((asset_id, taken_utc))

    out: dict[int, list[FaceMatch]] = {}
    for person in persons:
        matched: list[FaceMatch] = []
        for face in person.faces:
            if face.original_size is None:
                continue

            asset_id: str | None = None
            if face.capture_utc is not None:
                candidates = {
                    aid for aid, taken_utc in size_index.get(face.original_size, [])
                    if abs(taken_utc - face.capture_utc) <= _CAPTURE_TOLERANCE
                }
                if len(candidates) == 1:
                    asset_id = next(iter(candidates))

            if asset_id is None:
                hits = namesize_index.get((face.original_filename, face.original_size))
                if hits and len(hits) == 1:
                    asset_id = hits[0]

            if asset_id is not None:
                matched.append(FaceMatch(face=face, immich_asset_id=asset_id))
        out[person.apple_pk] = matched
    return out


# --- reconciling with Immich's existing face clusters ----------------------
#
# Immich's own ML face detection (`asset_face`, sourceType='machine-learning')
# already runs its own ArcFace clustering and groups faces under `person`
# rows — usually correctly, just anonymously (name=''). Rather than create
# brand-new Person rows and re-detect faces ourselves, we overlap each
# matched Apple face onto Immich's existing detections on that same asset:
# whichever existing (unnamed) person cluster keeps winning is almost
# certainly the same real person, and naming it retroactively names every
# other face already in that cluster too — not just the ones Apple tagged.


def apple_bbox_norm(face: AppleFace) -> tuple[float, float, float, float] | None:
    """Apple's (center_x, center_y, size) to a normalized top-left-origin
    (x1, y1, x2, y2) box, matching Immich's `asset_face` convention.

    Apple's ZCENTERX/ZCENTERY are bottom-left-origin normalized (same Vision
    framework convention `faces.detect()` already flips for Immich writes) —
    verified empirically: flipped Apple centers land within a few thousandths
    of the corresponding Immich ArcFace detection's center on the same asset.
    """
    if face.size <= 0:
        return None
    half = face.size / 2.0
    x1 = face.center_x - half
    x2 = face.center_x + half
    y1 = 1.0 - (face.center_y + half)
    y2 = 1.0 - (face.center_y - half)
    return (x1, y1, x2, y2)


@dataclass(frozen=True)
class ExistingFace:
    """One `asset_face` row on a matched asset, bbox normalized to 0..1."""

    face_id: str
    person_id: str | None
    person_name: str | None
    x1: float
    y1: float
    x2: float
    y2: float


# Two faces are "the same detection" if their box centers land within this
# many normalized units of each other. Empirically, a correctly flipped
# Apple center lands within ~0.01 of Immich's own detection; 0.05 leaves
# headroom for bbox-convention differences (Vision's looser crop vs
# RetinaFace's tighter one) while still rejecting a different face in a
# group photo (faces are rarely closer than that in frame).
_CENTER_MATCH_TOLERANCE = 0.05


def _box_center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _find_overlap(
    apple_box: tuple[float, float, float, float],
    candidates: list[ExistingFace],
) -> ExistingFace | None:
    ax, ay = _box_center(apple_box)
    best: ExistingFace | None = None
    best_dist = _CENTER_MATCH_TOLERANCE
    for ef in candidates:
        ex, ey = _box_center((ef.x1, ef.y1, ef.x2, ef.y2))
        dist = ((ax - ex) ** 2 + (ay - ey) ** 2) ** 0.5
        if dist <= best_dist:
            best = ef
            best_dist = dist
    return best


@dataclass
class PersonPlan:
    """What we'd do for one Apple-named person, before any DB write.

    `target_person_id` is the existing (currently unnamed) Immich person
    whose cluster this Apple person's faces overlap most — `None` if no
    single cluster clearly dominates (nothing to do yet; needs a human
    look, or a future "create a new person" path).
    """

    apple_pk: int
    full_name: str
    target_person_id: str | None
    target_votes: int
    total_votes: int
    orphan_face_ids: list[str] = field(default_factory=list)
    conflicts: list[tuple[FaceMatch, str, str]] = field(default_factory=list)
    already_named: int = 0
    no_detection: int = 0

    @property
    def confidence(self) -> float:
        return self.target_votes / self.total_votes if self.total_votes else 0.0


# A dominant cluster needs at least this many corroborating faces, and a
# clear majority over any other cluster it beat out — a single stray overlap
# (e.g. a mislabeled Immich cluster, or two people who look alike) shouldn't
# be enough to rename someone else's faces.
MIN_VOTES = 3
MIN_CONFIDENCE = 0.6


def build_person_plans(
    persons: list[ApplePerson],
    matches: dict[int, list[FaceMatch]],
    existing_faces_by_asset: dict[str, list[ExistingFace]],
) -> list[PersonPlan]:
    """For each Apple person, decide which existing Immich person cluster
    (if any) their matched faces overlap enough to name.

    Read-only / pure — callers apply `target_person_id` (name that person)
    and `orphan_face_ids` (attach to it) themselves.
    """
    plans: list[PersonPlan] = []
    for person in persons:
        face_matches = matches.get(person.apple_pk, [])
        votes: dict[str, int] = {}
        orphans: list[str] = []
        conflicts: list[tuple[FaceMatch, str, str]] = []
        already_named = 0
        no_detection = 0

        for fm in face_matches:
            apple_box = apple_bbox_norm(fm.face)
            if apple_box is None:
                no_detection += 1
                continue
            candidates = existing_faces_by_asset.get(fm.immich_asset_id, [])
            hit = _find_overlap(apple_box, candidates)
            if hit is None:
                no_detection += 1
                continue
            if hit.person_id is None:
                orphans.append(hit.face_id)
            elif not hit.person_name:
                votes[hit.person_id] = votes.get(hit.person_id, 0) + 1
            elif hit.person_name == person.full_name:
                already_named += 1
            else:
                conflicts.append((fm, hit.person_id, hit.person_name))

        target_person_id: str | None = None
        target_votes = 0
        total_votes = sum(votes.values())
        if votes:
            best_id, best_votes = max(votes.items(), key=lambda kv: kv[1])
            confidence = best_votes / total_votes
            if best_votes >= MIN_VOTES and confidence >= MIN_CONFIDENCE:
                target_person_id = best_id
                target_votes = best_votes

        # Orphan faces only get attached to a cluster we're actually naming.
        orphan_face_ids = orphans if target_person_id is not None else []

        plans.append(PersonPlan(
            apple_pk=person.apple_pk,
            full_name=person.full_name,
            target_person_id=target_person_id,
            target_votes=target_votes,
            total_votes=total_votes,
            orphan_face_ids=orphan_face_ids,
            conflicts=conflicts,
            already_named=already_named,
            no_detection=no_detection,
        ))
    return plans
