"""manifest.sqlite — the dedup pipeline's durable ledger.

Every file the consolidation pipeline has ever looked at gets exactly one
row here, keyed by absolute path, with a status that only moves forward:

    registered → fingerprinted → clustered → decided → promoted | quarantined

plus two statuses outside that flow: `canonical` (already in
`library/originals/`, seeded by `immy dedup bootstrap` — the corpus new
arrivals compare against) and `error` (fingerprint failed; kept so re-runs
don't retry a corrupt file forever without being told to).

The manifest is what makes every mode idempotent and resumable: backlog
re-runs skip decided clusters, incremental runs only touch paths newer
than the per-source watermark, and CLIP embeddings are cached here so no
image is ever embedded twice (Stage C is the expensive stage; the cache
is what makes steady-state incremental runs nearly free).

Follows snapshot.py's sqlite conventions (IF NOT EXISTS schema, meta
key/value table, explicit schema version). WAL mode because `status` is
read by a human running `immy dedup status` while a long
fingerprint/cluster pass writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..exif import MEDIA_EXTS

SCHEMA_VERSION = 4

# status lifecycle values (kept as plain strings in the DB)
REGISTERED = "registered"
FINGERPRINTED = "fingerprinted"
CLUSTERED = "clustered"
DECIDED = "decided"
PROMOTED = "promoted"
QUARANTINED = "quarantined"
CANONICAL = "canonical"
ERROR = "error"
# v4: the library already holds this file's exact bytes (see library_file);
# `dedup apply` moves it to quarantine and it ends as `quarantined`.
ALIAS = "alias"

# v4 identity columns, in the order they are added to an existing manifest.
# Brand-new manifests get them from the CREATE TABLE text below; old ones get
# them from `_migrate`. Keep the two lists in step.
V4_ASSET_COLUMNS = (
    ("source_uid", "TEXT"),
    ("component", "TEXT"),
    ("sha256", "TEXT"),
    ("dest_path", "TEXT"),
    ("alias_path", "TEXT"),
)

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
  id           INTEGER PRIMARY KEY,
  source       TEXT NOT NULL,      -- icloud | google | originals
  path         TEXT NOT NULL UNIQUE,
  status       TEXT NOT NULL,
  bytes        INTEGER,
  mtime        REAL,
  media_type   TEXT,               -- image | video
  format       TEXT,               -- lowercase extension, no dot
  width        INTEGER,
  height       INTEGER,
  taken_at     TEXT,               -- ISO8601, naive local as shot (dates.py convention)
  taken_src    TEXT,               -- exif | json | filename | mtime — provenance matters
                                   -- for Stage A trust and google metadata rescue
  gps_lat      REAL,
  gps_lon      REAL,
  phash        TEXT,               -- 16 hex chars; NULL for videos (v1: metadata-only)
  exif_fields  INTEGER,
  burst_uuid   TEXT,
  live_cid     TEXT,               -- Apple ContentIdentifier (Live Photo pair glue)
  edited       INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  -- v4 identity (todo/PHASE2-IDENTITY-DESIGN.md). Indexes on these live in
  -- _CREATE_INDEXES: this script runs BEFORE the version check, and an old
  -- manifest has none of these columns until _migrate adds them.
  source_uid   TEXT,               -- the source's own id (Photos UUID); NULL if none
  component    TEXT,               -- original | live_video | raw | edited; set iff source_uid
  sha256       TEXT,               -- full-content hash while immy held the file
  dest_path    TEXT,               -- where apply/promote-rest put it; written before unlink
  alias_path   TEXT                -- status alias: the library_file whose bytes these are
);
CREATE INDEX IF NOT EXISTS idx_asset_status   ON asset (status);
CREATE INDEX IF NOT EXISTS idx_asset_source   ON asset (source, status);
CREATE INDEX IF NOT EXISTS idx_asset_taken    ON asset (taken_at);
CREATE INDEX IF NOT EXISTS idx_asset_live_cid ON asset (live_cid);

-- v4: what the library holds, by content. Independent of asset rows on
-- purpose — a promoted row's history says where a file went once, this says
-- what is there. (bytes, mtime_ns, inode) is a cache key for skipping
-- re-hashes, NOT proof: anything that disposes of a file on the strength of
-- a match re-hashes both sides first (see dedup/identity.py).
CREATE TABLE IF NOT EXISTS library_file (
  path       TEXT PRIMARY KEY,
  bytes      INTEGER NOT NULL,
  mtime_ns   INTEGER NOT NULL,
  inode      INTEGER NOT NULL,
  sha256     TEXT NOT NULL,
  hashed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster (
  id               INTEGER PRIMARY KEY,
  winner_asset_id  INTEGER,
  confidence       REAL,
  decision         TEXT NOT NULL DEFAULT 'pending',  -- pending | auto | review | kept_all
  clip_cos_sim     REAL  -- Stage C: min(cosine(winner, member)) over image members;
                          -- NULL until `dedup confirm` visits this cluster.
);

CREATE TABLE IF NOT EXISTS membership (
  cluster_id  INTEGER NOT NULL REFERENCES cluster(id),
  asset_id    INTEGER NOT NULL UNIQUE REFERENCES asset(id),
  role        TEXT NOT NULL DEFAULT 'member',       -- member | winner | loser
  PRIMARY KEY (cluster_id, asset_id)
);

-- CLIP embedding cache: computed at most once per asset, ever.
CREATE TABLE IF NOT EXISTS embedding (
  asset_id  INTEGER PRIMARY KEY REFERENCES asset(id),
  model     TEXT NOT NULL,
  vec       BLOB NOT NULL                            -- float32[dim], raw bytes
);

CREATE TABLE IF NOT EXISTS meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

-- Footage triage (see immy/TRIAGE.md). `video_signal` is scan-derived and
-- always safe to rebuild; `triage` holds real verdicts (human or executor)
-- and is the ONLY table the future apply step will read.
CREATE TABLE IF NOT EXISTS triage (
  asset_id    INTEGER PRIMARY KEY REFERENCES asset(id),
  verdict     TEXT NOT NULL CHECK (verdict IN ('keep','compress','cold','trash')),
  reason      TEXT,
  decided_by  TEXT NOT NULL,      -- 'human' | rule name
  decided_at  TEXT NOT NULL,      -- ISO8601
  applied_at  TEXT                -- set by the (future) executor; NULL = pending
);

CREATE TABLE IF NOT EXISTS video_signal (
  asset_id       INTEGER PRIMARY KEY REFERENCES asset(id),
  duration_s     REAL,
  codec          TEXT,
  bitrate_kbps   REAL,
  take_group     INTEGER,          -- clips shot in one burst share a group
  favorite       INTEGER,          -- Immich isFavorite; NULL = not looked up
  album_count    INTEGER,
  frames_json    TEXT,             -- sampled-frame paths relative to frames root
  suggested      TEXT,             -- advisory: keep | compress | review-take
  suggest_reason TEXT
);
"""


@dataclass(frozen=True)
class RegisterResult:
    new: int
    already_known: int
    skipped_young: int


_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_asset_identity
  ON asset (source, source_uid, component) WHERE source_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_asset_sha256 ON asset (sha256) WHERE sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_library_file_sha256 ON library_file (sha256);
"""


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns) -> None:
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Column additions for existing manifests — `CREATE TABLE IF NOT EXISTS`
    only handles brand-new databases, so schema growth on a live manifest
    (n5's carries 290k+ rows) needs an explicit ALTER TABLE.

    Every step inspects `PRAGMA table_info` first, so running it against a
    manifest that already has some or all columns is a no-op for those. Does
    NOT commit: `open_manifest` runs it and the version bump inside one
    transaction, so an interrupt leaves either the old version with none of
    the new columns or the new version with all of them."""
    _add_missing_columns(conn, "cluster", [("clip_cos_sim", "REAL")])  # v2
    # v3 (triage + video_signal) is whole tables only — _CREATE_SCHEMA made them.
    _add_missing_columns(conn, "asset", V4_ASSET_COLUMNS)              # v4


def open_manifest(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # confirm_clip holds a write lock across each network-bound embed call;
    # without this a concurrent writer (e.g. `decide` running at the same
    # time) gets an immediate "database is locked" instead of just waiting.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_CREATE_SCHEMA)  # commits; runs strictly before the migration
    existing = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    current = int(existing[0]) if existing else None
    if current is not None and current > SCHEMA_VERSION:
        raise RuntimeError(
            f"manifest schema v{current} is newer than this immy (v{SCHEMA_VERSION})"
        )
    if current is None or current < SCHEMA_VERSION:
        # A missing version row is inspected, not trusted to mean "fresh":
        # _migrate is a no-op against a schema that already has every column.
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate(conn)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    conn.executescript(_CREATE_INDEXES)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def watermark_key(source: str) -> str:
    return f"watermark:{source}"


def register(
    conn: sqlite3.Connection,
    source: str,
    root: Path,
    *,
    min_age_hours: float = 0.0,
    status: str = REGISTERED,
) -> RegisterResult:
    """Walk `root` and insert one row per unseen media file.

    `min_age_hours` is the incremental mode's settle gate: icloudpd lands
    Live Photo pairs and edits non-atomically, so files younger than the
    gate are left for the next run rather than half-ingested. Google
    `*.json` sidecars are skipped here — they're read as companions during
    fingerprinting, not tracked as assets.

    Also advances the per-source watermark (max mtime actually accepted),
    which incremental mode uses only as a fast-path hint — registration is
    still a full walk with INSERT-or-skip, so a file that arrived with an
    old mtime is never lost to a watermark race.
    """
    cutoff = time.time() - min_age_hours * 3600
    new = known = young = 0
    max_mtime = float(get_meta(conn, watermark_key(source)) or 0.0)

    for entry in sorted(root.rglob("*")):
        if not entry.is_file() or entry.suffix.lower() not in MEDIA_EXTS:
            continue
        stat = entry.stat()
        if stat.st_mtime > cutoff:
            young += 1
            continue
        cursor = conn.execute(
            "INSERT INTO asset (source, path, status, bytes, mtime, format) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO NOTHING",
            (
                source,
                str(entry),
                status,
                stat.st_size,
                stat.st_mtime,
                entry.suffix.lower().lstrip("."),
            ),
        )
        if cursor.rowcount:
            new += 1
            max_mtime = max(max_mtime, stat.st_mtime)
        else:
            known += 1

    if new:
        set_meta(conn, watermark_key(source), str(max_mtime))
    conn.commit()
    return RegisterResult(new=new, already_known=known, skipped_young=young)


def pending_fingerprint(
    conn: sqlite3.Connection, *, source: str | None = None, limit: int | None = None
) -> list[tuple[int, str, str]]:
    """(id, path, source) rows still awaiting metadata + pHash."""
    sql = "SELECT id, path, source FROM asset WHERE status=?"
    params: list = [REGISTERED]
    if source:
        sql += " AND source=?"
        params.append(source)
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def write_fingerprint(
    conn: sqlite3.Connection,
    asset_id: int,
    fields: dict,
    *,
    status: str = FINGERPRINTED,
) -> bool:
    """Advance one `registered` asset to `status` (normally `fingerprinted`;
    `alias` when the library already holds its bytes) with its extracted
    metadata.

    `fields` keys must be column names; whitelisted here so a typo fails
    loudly instead of writing nothing. A `source_uid` without a `component`
    is refused: identity is the pair, and a NULL component would let two
    rows claim one UID. Conditional on the row still being `registered` —
    returns False (and writes nothing) if another writer got there first.
    """
    allowed = {
        "media_type", "width", "height", "taken_at", "taken_src",
        "gps_lat", "gps_lon", "phash", "exif_fields",
        "burst_uuid", "live_cid", "edited",
        "sha256", "source_uid", "component", "alias_path",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown fingerprint fields: {bad}")
    if fields.get("source_uid") and not fields.get("component"):
        raise ValueError("source_uid requires a component")
    sets = ", ".join(f"{k}=?" for k in fields)
    cursor = conn.execute(
        f"UPDATE asset SET {sets}, status=?, error=NULL WHERE id=? AND status=?",
        [*fields.values(), status, asset_id, REGISTERED],
    )
    return cursor.rowcount == 1


def write_error(
    conn: sqlite3.Connection, asset_id: int, message: str, *, only_if: str | None = None,
) -> bool:
    """Mark one asset `error`. `only_if` makes it conditional on the current
    status (fingerprint passes REGISTERED, so a worker holding a stale
    pending list cannot overwrite a row another worker already advanced).
    Returns whether the row was written."""
    sql, params = "UPDATE asset SET status=?, error=? WHERE id=?", [ERROR, message[:500], asset_id]
    if only_if is not None:
        sql += " AND status=?"
        params.append(only_if)
    return conn.execute(sql, params).rowcount == 1


def retry_errors(conn: sqlite3.Connection, *, match: str | None = None) -> int:
    """Send `error` rows back to `registered` so the next fingerprint pass
    retries them — the way out for a stub that has since been replaced by
    the real file. `bytes`/`mtime` are refreshed from disk (a replaced file
    at the same path is otherwise invisible: `register` keys on path).
    Rows whose file is gone stay `error`. Returns the count reset."""
    sql = "SELECT id, path FROM asset WHERE status=?"
    params: list = [ERROR]
    if match:
        sql += " AND error LIKE ?"
        params.append(f"%{match}%")
    reset = 0
    for asset_id, path in conn.execute(sql, params).fetchall():
        try:
            st = Path(path).stat()
        except OSError:
            continue
        conn.execute(
            "UPDATE asset SET status=?, error=NULL, bytes=?, mtime=? WHERE id=? AND status=?",
            (REGISTERED, st.st_size, st.st_mtime, asset_id, ERROR),
        )
        reset += 1
    conn.commit()
    return reset


def upsert_library_file(
    conn: sqlite3.Connection, path: Path, sha256: str, st: os.stat_result,
) -> None:
    """Record that the library holds `path` with these bytes, keyed by the
    stat taken around the hash (`identity.hash_stable`). Does not commit."""
    conn.execute(
        "INSERT INTO library_file (path, bytes, mtime_ns, inode, sha256, hashed_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
        "bytes=excluded.bytes, mtime_ns=excluded.mtime_ns, inode=excluded.inode, "
        "sha256=excluded.sha256, hashed_at=excluded.hashed_at",
        (str(path), st.st_size, st.st_mtime_ns, st.st_ino, sha256,
         time.strftime("%Y-%m-%dT%H:%M:%S")),
    )


def get_embedding(conn: sqlite3.Connection, asset_id: int, model: str) -> list[float] | None:
    """Cached CLIP vector for one asset, or None if never embedded with
    this exact model (a model switch re-embeds — the cache key is
    (asset_id) only, so callers must not mix models against one manifest
    without wiping the table first)."""
    row = conn.execute(
        "SELECT model, vec FROM embedding WHERE asset_id=?", (asset_id,)
    ).fetchone()
    if row is None or row[0] != model:
        return None
    import numpy as np
    return np.frombuffer(row[1], dtype=np.float32).tolist()


def set_embedding(conn: sqlite3.Connection, asset_id: int, model: str, vec: list[float]) -> None:
    import numpy as np
    blob = np.asarray(vec, dtype=np.float32).tobytes()
    conn.execute(
        "INSERT INTO embedding (asset_id, model, vec) VALUES (?, ?, ?) "
        "ON CONFLICT(asset_id) DO UPDATE SET model=excluded.model, vec=excluded.vec",
        (asset_id, model, blob),
    )


def stats(conn: sqlite3.Connection) -> dict:
    """Counts for `immy dedup status`: per source × status, plus decisions."""
    by_state: dict[str, dict[str, int]] = {}
    for source, status, count in conn.execute(
        "SELECT source, status, COUNT(*) FROM asset GROUP BY source, status"
    ):
        by_state.setdefault(source, {})[status] = count
    decisions = dict(
        conn.execute("SELECT decision, COUNT(*) FROM cluster GROUP BY decision")
    )
    embedded = conn.execute("SELECT COUNT(*) FROM embedding").fetchone()[0]
    return {"assets": by_state, "clusters": decisions, "embeddings": embedded}


def export_stats_json(conn: sqlite3.Connection) -> str:
    return json.dumps(stats(conn), indent=2, sort_keys=True)
