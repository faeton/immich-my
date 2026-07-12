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
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..exif import MEDIA_EXTS

SCHEMA_VERSION = 2

# status lifecycle values (kept as plain strings in the DB)
REGISTERED = "registered"
FINGERPRINTED = "fingerprinted"
CLUSTERED = "clustered"
DECIDED = "decided"
PROMOTED = "promoted"
QUARANTINED = "quarantined"
CANONICAL = "canonical"
ERROR = "error"

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
  error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_status   ON asset (status);
CREATE INDEX IF NOT EXISTS idx_asset_source   ON asset (source, status);
CREATE INDEX IF NOT EXISTS idx_asset_taken    ON asset (taken_at);
CREATE INDEX IF NOT EXISTS idx_asset_live_cid ON asset (live_cid);

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
"""


@dataclass(frozen=True)
class RegisterResult:
    new: int
    already_known: int
    skipped_young: int


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Column additions for existing manifests — `CREATE TABLE IF NOT EXISTS`
    only handles brand-new databases, so schema growth on a live manifest
    (e.g. n5's, already carrying 270k+ rows) needs an explicit ALTER TABLE."""
    if from_version < 2:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(cluster)")}
        if "clip_cos_sim" not in cols:
            conn.execute("ALTER TABLE cluster ADD COLUMN clip_cos_sim REAL")
    conn.commit()


def open_manifest(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # confirm_clip holds a write lock across each network-bound embed call;
    # without this a concurrent writer (e.g. `decide` running at the same
    # time) gets an immediate "database is locked" instead of just waiting.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_CREATE_SCHEMA)
    existing = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
    else:
        current = int(existing[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"manifest schema v{current} is newer than this immy (v{SCHEMA_VERSION})"
            )
        if current < SCHEMA_VERSION:
            _migrate(conn, current)
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
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


def write_fingerprint(conn: sqlite3.Connection, asset_id: int, fields: dict) -> None:
    """Advance one asset to `fingerprinted` with its extracted metadata.

    `fields` keys must be column names; whitelisted here so a typo fails
    loudly instead of writing nothing.
    """
    allowed = {
        "media_type", "width", "height", "taken_at", "taken_src",
        "gps_lat", "gps_lon", "phash", "exif_fields",
        "burst_uuid", "live_cid", "edited",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown fingerprint fields: {bad}")
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE asset SET {sets}, status=?, error=NULL WHERE id=?",
        [*fields.values(), FINGERPRINTED, asset_id],
    )


def write_error(conn: sqlite3.Connection, asset_id: int, message: str) -> None:
    conn.execute(
        "UPDATE asset SET status=?, error=? WHERE id=?",
        (ERROR, message[:500], asset_id),
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
