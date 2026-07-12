"""Dedup cascade engine — Stages A/B/C/D over the manifest.

Implements the locked cascade from raw/CONSOLIDATION-PIPELINE.md:

    Stage A  block      EXIF time ±3s | day + ~100m GPS cell | filename stem
    Stage B  prefilter  pHash Hamming ≤10 candidate, ≤6 strong
    Stage C  confirm    CLIP cosine (confirm_clip) — a narrow, high-bar
                        auto-confirm gate (CLIP_AUTO_THRESHOLD, calibrated
                        2026-07-11 on 300 hand-labeled pairs from the live
                        review queue). CLIP embeds scene similarity, not
                        photographic identity, so it only safely resolves a
                        small slice of the pHash-ambiguous zone — everything
                        below the bar still lands in `review`, same as
                        before Stage C existed.
    Stage D  decide     auto-merge only on strong pHash (or a Stage C
                        confirm) + ≥1 agreeing metadata signal, and never
                        past the guard rails (burst / Live pair / edited /
                        aspect change).

Key scope choices for v1, all deliberate:
- Videos get no pHash (no frame decode). They cluster only on exact byte
  size + timestamp agreement and everything else routes to review. Video
  dupes across iCloud/Google are almost always byte-identical or
  container-transcoded; the transcode case needs eyes anyway.
- A cluster whose winner would displace a `canonical` asset (one already
  in library/originals/) routes to review, never auto — replacing a file
  Immich already serves is a swap, not a promote, and deserves a human.
- Blocks larger than MAX_BLOCK are skipped and reported, not silently
  pair-exploded (a generic filename stem like "image" could otherwise
  produce millions of pairs).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path

from . import manifest, phash
from .. import clip as clip_mod
from .. import dates
from .. import sidecar
from ..exif import ExifRow

HAMMING_CANDIDATE = 10
HAMMING_STRONG = 6

# Stage C auto-confirm bar, calibrated 2026-07-11 on 300 hand-labeled pairs
# drawn from the live review queue (raw/CONSOLIDATION-PIPELINE.md dedup
# pass). CLIP (ViT-B-32__openai, Immich's own weights) embeds *scene*
# similarity, not photographic identity — two different shots of the same
# subject a few seconds apart score nearly as high as true re-exports of one
# shot. In the actual hamming 7-10 ambiguity zone, precision only clears
# ~90% once cos_sim reaches this bar, and recall there is intentionally
# small (~10-15%): this is a narrow, high-confidence gate, not a general
# resolver for the review queue. Below it, clusters stay in `review` exactly
# as before Stage C existed — no regression, modest reduction in review load.
CLIP_AUTO_THRESHOLD = 0.98

TIME_BLOCK_SECONDS = 3
GEO_CELL_DEG = 0.001          # ~100 m at mid latitudes
MAX_BLOCK = 200               # bail-out for degenerate blocks

# Generic camera/phone filename counters (IMG_0001.MOV...) reset and repeat
# over years of device use, so a bare stem match on two videos years apart is
# coincidence, not a dupe candidate — and unlike images, videos get no pHash
# to reject the false pair later (no frame decode in v1). Diagnosed
# 2026-07-12: a single 458-member cluster spanned 6,474 days (17.7y) via
# chained stem-only blocks. Gate stem-driven video pairs on a plausible date
# gap; only when BOTH sides have a real capture time (exif/json, not the
# mtime fallback, which reflects copy time not shoot time).
VIDEO_STEM_PLAUSIBILITY_SECONDS = 60 * 86400

VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "mts", "m2ts", "insv", "lrv", "lrf"}

# A camera/drone shooting RAW+JPEG simultaneously produces two same-stem,
# same-directory files that are two formats of ONE capture, not a duplicate
# pair -- both are always meant to be kept, unlike a true cross-source
# re-export duplicate. RAW decodes to a preview visually near-identical to
# its JPEG twin, so without this check pHash correctly (but unhelpfully)
# flags them as a "strong" duplicate pair. Diagnosed 2026-07-12: 577 review
# clusters were just RAW+JPEG twins, permanently stuck in review because the
# `member.source == 'originals'` guard in _decide_one can never let them
# auto-resolve.
RAW_EXTS = {"dng", "cr2", "cr3", "arw", "nef", "raf", "rw2", "orf"}
JPEG_EXTS = {"jpg", "jpeg", "heic", "heif"}

SOURCE_WEIGHT = {"originals": 120, "icloud": 100, "google": 30}
FORMAT_BONUS = {
    "heic": 20, "heif": 20, "dng": 20, "cr2": 20, "cr3": 20,
    "arw": 20, "nef": 20, "raf": 20, "rw2": 20, "orf": 20,
}

# Google Takeout marks edited exports with a filename suffix; Apple marks
# adjustments in XMP. Either flag flips `edited` (a never-auto-merge guard).
_EDITED_NAME_RE = re.compile(r"-(edited|effects)$", re.IGNORECASE)
# Takeout numbers filename collisions as "IMG_1234(1).JPG"; the copy marker
# is noise for stem blocking.
_COPY_MARKER_RE = re.compile(r"\(\d+\)$")


# ---------------------------------------------------------------- fingerprint


def _google_json_companion(path: Path) -> dict | None:
    """Locate and parse a Takeout `*.json` sidecar for a media file.

    Takeout naming has three common shapes: `<name>.json`,
    `<name>.supplemental-metadata.json`, and a truncated form when the
    combined name would exceed Google's ~46-char limit. Try exact forms
    first, then fall back to a prefix glob.
    """
    candidates = [
        path.with_name(path.name + ".json"),
        path.with_name(path.name + ".supplemental-metadata.json"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return _read_json(candidate)
    for sibling in path.parent.glob(path.stem[:20] + "*.json"):
        stem = sibling.name.removesuffix(".json").removesuffix(".supplemental-metadata")
        if path.name.startswith(stem) or stem.startswith(path.name):
            return _read_json(sibling)
    return None


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _first_int(row: ExifRow, *keys: str) -> int | None:
    value = row.get(*keys)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_float(row: ExifRow, *keys: str) -> float | None:
    value = row.get(*keys)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fingerprint_fields(row: ExifRow, source: str) -> dict:
    """Extract the manifest's fingerprint columns from one exiftool row.

    Pure metadata — pHash is added by the caller (it needs a decode and
    its own error handling)."""
    path = row.path
    ext = path.suffix.lower().lstrip(".")
    media_type = "video" if ext in VIDEO_EXTS else "image"

    authority = dates.resolve(row)
    taken_at = authority.dt.isoformat() if authority else None
    taken_src = authority.source if authority else None

    lat = _first_float(row, "Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude")
    lon = _first_float(row, "Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude")
    if lat is not None and lon is not None and abs(lat) < 1e-3 and abs(lon) < 1e-3:
        lat = lon = None  # null-island sensor artifact, same rule as exif.py

    # Google JSON sidecar outranks filename/mtime guesses (it holds the
    # actual photoTakenTime even when Takeout stripped the EXIF), but
    # never outranks real EXIF.
    if source == "google" and (taken_src in (None, "filename", "mtime") or lat is None):
        sidecar = _google_json_companion(path)
        if sidecar:
            if taken_src in (None, "filename", "mtime"):
                ts = (sidecar.get("photoTakenTime") or {}).get("timestamp")
                if ts:
                    taken_at = datetime.utcfromtimestamp(int(ts)).isoformat()
                    taken_src = "json"
            if lat is None:
                geo = sidecar.get("geoData") or {}
                glat, glon = geo.get("latitude"), geo.get("longitude")
                if glat and glon and not (abs(glat) < 1e-3 and abs(glon) < 1e-3):
                    lat, lon = float(glat), float(glon)

    edited = bool(
        _EDITED_NAME_RE.search(path.stem)
        or row.get("XMP:AdjustmentTimestamp", "MakerNotes:AdjustmentVersion")
    )

    return {
        "media_type": media_type,
        "width": _first_int(
            row, "File:ImageWidth", "EXIF:ExifImageWidth", "EXIF:ImageWidth",
            "QuickTime:ImageWidth", "QuickTime:SourceImageWidth",
        ),
        "height": _first_int(
            row, "File:ImageHeight", "EXIF:ExifImageHeight", "EXIF:ImageHeight",
            "QuickTime:ImageHeight", "QuickTime:SourceImageHeight",
        ),
        "taken_at": taken_at,
        "taken_src": taken_src,
        "gps_lat": lat,
        "gps_lon": lon,
        "exif_fields": len(row.raw),
        "burst_uuid": row.get("MakerNotes:BurstUUID"),
        "live_cid": row.get("MakerNotes:ContentIdentifier", "QuickTime:ContentIdentifier"),
        "edited": int(edited),
    }


def fingerprint_pending(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    batch_size: int = 200,
    progress=None,
) -> tuple[int, int]:
    """Fingerprint every `registered` asset: exiftool batch + pHash.

    Returns (ok, failed). Commits per batch so a crash resumes at the
    batch boundary, not from zero."""
    import exiftool

    pending = manifest.pending_fingerprint(conn, source=source)
    ok = failed = 0

    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        paths = [p for _, p, _ in batch]
        with exiftool.ExifToolHelper(
            common_args=["-G", "-n", "-fast2", "-m"], check_execute=False,
        ) as et:
            try:
                blobs = et.get_metadata(paths)
            except Exception:
                blobs = []
                for target in paths:
                    try:
                        blobs.extend(et.get_metadata([target]))
                    except Exception:
                        pass
        by_path = {blob["SourceFile"]: blob for blob in blobs if "SourceFile" in blob}

        for asset_id, path_text, asset_source in batch:
            path = Path(path_text)
            raw = by_path.get(path_text)
            if raw is None:
                manifest.write_error(conn, asset_id, "exiftool read failed")
                failed += 1
                continue
            try:
                fields = fingerprint_fields(ExifRow(path=path, raw=raw), asset_source)
                if fields["media_type"] == "image":
                    fields["phash"] = phash.to_hex(phash.phash_file(path))
                manifest.write_fingerprint(conn, asset_id, fields)
                ok += 1
            except Exception as exc:  # corrupt file, undecodable HEIC, …
                manifest.write_error(conn, asset_id, f"{type(exc).__name__}: {exc}")
                failed += 1
        conn.commit()
        if progress:
            progress(min(start + batch_size, len(pending)), len(pending))
    return ok, failed


# ------------------------------------------------------------------- blocking


@dataclass(frozen=True)
class AssetLite:
    """One asset's cluster-relevant columns, loaded once, hashable."""
    id: int
    source: str
    path: str
    bytes: int
    media_type: str
    format: str
    width: int | None
    height: int | None
    taken_at: str | None
    taken_src: str | None
    gps_lat: float | None
    gps_lon: float | None
    phash: int | None
    exif_fields: int
    burst_uuid: str | None
    live_cid: str | None
    edited: bool

    @property
    def epoch(self) -> float | None:
        if not self.taken_at:
            return None
        try:
            return datetime.fromisoformat(self.taken_at).timestamp()
        except ValueError:
            return None


def load_clusterable(conn: sqlite3.Connection) -> list[AssetLite]:
    """Fingerprinted + canonical assets — the clustering universe.

    `decided`/`promoted`/`quarantined` rows are excluded: their clusters
    are settled and re-running must not reopen them."""
    rows = conn.execute(
        "SELECT id, source, path, bytes, media_type, format, width, height,"
        "       taken_at, taken_src, gps_lat, gps_lon, phash, exif_fields,"
        "       burst_uuid, live_cid, edited"
        "  FROM asset WHERE status IN (?, ?, ?)",
        (manifest.FINGERPRINTED, manifest.CLUSTERED, manifest.CANONICAL),
    ).fetchall()
    return [
        AssetLite(
            id=r[0], source=r[1], path=r[2], bytes=r[3] or 0, media_type=r[4] or "image",
            format=r[5] or "", width=r[6], height=r[7], taken_at=r[8], taken_src=r[9],
            gps_lat=r[10], gps_lon=r[11],
            phash=phash.from_hex(r[12]) if r[12] else None,
            exif_fields=r[13] or 0, burst_uuid=r[14], live_cid=r[15], edited=bool(r[16]),
        )
        for r in rows
    ]


def normalized_stem(path: str) -> str:
    stem = Path(path).stem.lower()
    stem = _EDITED_NAME_RE.sub("", stem)
    stem = _COPY_MARKER_RE.sub("", stem)
    return stem.strip("._- ")


def candidate_pairs(assets: list[AssetLite]) -> tuple[set[tuple[int, int]], list[str]]:
    """Stage A: emit id pairs sharing any block. Returns (pairs, warnings)."""
    by_id = {a.id: a for a in assets}
    blocks: dict[str, list[int]] = {}

    timed = sorted(
        (a for a in assets if a.epoch is not None and a.taken_src in ("exif", "json")),
        key=lambda a: a.epoch,
    )
    pairs: set[tuple[int, int]] = set()
    # Time proximity: sliding window over the sorted-by-time list — exact
    # ±3s without bucket-boundary misses.
    for i, left in enumerate(timed):
        for right in timed[i + 1:]:
            if right.epoch - left.epoch > TIME_BLOCK_SECONDS:
                break
            pairs.add((min(left.id, right.id), max(left.id, right.id)))

    for a in assets:
        stem = normalized_stem(a.path)
        if len(stem) >= 4:
            blocks.setdefault(f"stem:{stem}", []).append(a.id)
        if a.epoch is not None and a.gps_lat is not None and a.gps_lon is not None:
            day = a.taken_at[:10]
            cell = (
                math.floor(a.gps_lat / GEO_CELL_DEG),
                math.floor(a.gps_lon / GEO_CELL_DEG),
            )
            blocks.setdefault(f"geo:{day}:{cell[0]}:{cell[1]}", []).append(a.id)

    warnings: list[str] = []
    for key, ids in blocks.items():
        if len(ids) < 2:
            continue
        if len(ids) > MAX_BLOCK:
            warnings.append(f"block {key} has {len(ids)} members — skipped")
            continue
        for left, right in combinations(sorted(ids), 2):
            pairs.add((left, right))

    # A pair is only meaningful across… anything. Same-source pairs are
    # valid too (iCloud bursts, Takeout re-exports).
    return {p for p in pairs if p[0] in by_id and p[1] in by_id}, warnings


# ----------------------------------------------------------------- clustering


class _UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def _is_raw_jpeg_companion(a: AssetLite, b: AssetLite) -> bool:
    is_raw_jpeg_pair = (
        (a.format in RAW_EXTS and b.format in JPEG_EXTS)
        or (a.format in JPEG_EXTS and b.format in RAW_EXTS)
    )
    if not is_raw_jpeg_pair:
        return False
    return (
        Path(a.path).parent == Path(b.path).parent
        and normalized_stem(a.path) == normalized_stem(b.path)
    )


def _pair_evidence(a: AssetLite, b: AssetLite) -> tuple[str, int | None] | None:
    """Stage B verdict for one candidate pair.

    Returns (tier, hamming) — tier 'strong'|'candidate' — or None (not a
    dupe pair)."""
    if _is_raw_jpeg_companion(a, b):
        return None
    if a.media_type == "image" and b.media_type == "image":
        if a.phash is None or b.phash is None:
            return None
        distance = phash.hamming(a.phash, b.phash)
        if distance <= HAMMING_STRONG:
            return ("strong", distance)
        if distance <= HAMMING_CANDIDATE:
            return ("candidate", distance)
        return None
    if a.media_type == "video" and b.media_type == "video":
        # v1: no frame decode for videos, so a shared block (often just a
        # generic recycled filename stem) is only trustworthy when we can
        # actually check it: byte-identical (definite dupe, works
        # regardless of metadata quality) or two INDEPENDENTLY reliable
        # capture times (exif/json — never the mtime fallback, which
        # reflects copy/unpack time, not shoot time) that are close
        # together. Diagnosed 2026-07-12: generic stems like IMG_0XXX
        # recur for 15+ years of phone use; on unreliable-timestamp
        # videos a bare block match is coincidence, not evidence, and was
        # chaining thousands of unrelated clips into single clusters.
        if a.bytes and a.bytes == b.bytes:
            return ("strong", None)
        reliable = (
            a.epoch is not None and a.taken_src in ("exif", "json", "companion")
            and b.epoch is not None and b.taken_src in ("exif", "json", "companion")
        )
        if not reliable or abs(a.epoch - b.epoch) > VIDEO_STEM_PLAUSIBILITY_SECONDS:
            return None
        return ("candidate", None)
    return None  # image↔video never pairs (Live Photo halves are a pair, not dupes)


def cluster(conn: sqlite3.Connection) -> dict:
    """Stage A+B: block, pHash-filter, union-find, persist clusters.

    Idempotent by construction: only `fingerprinted`/`canonical` assets
    without an existing membership row enter; assets already claimed by a
    cluster are skipped."""
    assets = load_clusterable(conn)
    claimed = {
        row[0] for row in conn.execute("SELECT asset_id FROM membership")
    }
    universe = [a for a in assets if a.id not in claimed or a.source == "originals"]
    by_id = {a.id: a for a in universe}

    pairs, warnings = candidate_pairs(universe)
    uf = _UnionFind()
    evidence: dict[tuple[int, int], tuple[str, int | None]] = {}
    for left, right in pairs:
        verdict = _pair_evidence(by_id[left], by_id[right])
        if verdict:
            evidence[(left, right)] = verdict
            uf.union(left, right)

    groups: dict[int, list[int]] = {}
    for asset_id in uf.parent:
        groups.setdefault(uf.find(asset_id), []).append(asset_id)

    created = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # Skip groups that already map 1:1 onto an existing cluster
        # (re-run after new arrivals extends clusters instead).
        existing = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT cluster_id FROM membership "
                f"WHERE asset_id IN ({','.join('?' * len(members))})",
                members,
            )
        }
        if existing:
            cluster_id = existing.pop()  # merge into the first; others re-pointed
            for stale in existing:
                conn.execute(
                    "UPDATE membership SET cluster_id=? WHERE cluster_id=?",
                    (cluster_id, stale),
                )
                conn.execute("DELETE FROM cluster WHERE id=?", (stale,))
        else:
            cursor = conn.execute("INSERT INTO cluster (decision) VALUES ('pending')")
            cluster_id = cursor.lastrowid
            created += 1
        for asset_id in members:
            conn.execute(
                "INSERT INTO membership (cluster_id, asset_id) VALUES (?, ?) "
                "ON CONFLICT(asset_id) DO NOTHING",
                (cluster_id, asset_id),
            )
            conn.execute(
                "UPDATE asset SET status=? WHERE id=? AND status=?",
                (manifest.CLUSTERED, asset_id, manifest.FINGERPRINTED),
            )
    conn.commit()
    return {
        "universe": len(universe),
        "pairs_blocked": len(pairs),
        "pairs_confirmed": len(evidence),
        "clusters_created": created,
        "warnings": warnings,
    }


# --------------------------------------------------------------- CLIP confirm


def _clip_ready_clusters(conn: sqlite3.Connection) -> list[int]:
    """Clusters worth spending a CLIP call on: undecided or already `review`,
    at least 2 image members, no `clip_cos_sim` yet. Guard-triggered review
    (burst/edited/aspect/originals-swap) still gets a cos_sim recorded — it
    won't flip the decision (the guard runs first in `_decide_one`), but the
    number is useful for the human-review sort/triage view."""
    rows = conn.execute(
        """
        SELECT c.id, COUNT(*) AS n_images
        FROM cluster c
        JOIN membership m ON m.cluster_id = c.id
        JOIN asset a ON a.id = m.asset_id
        WHERE c.decision IN ('pending', 'review')
          AND c.clip_cos_sim IS NULL
          AND a.media_type = 'image'
        GROUP BY c.id
        HAVING n_images >= 2
        ORDER BY c.id
        """
    ).fetchall()
    return [r[0] for r in rows]


def _prepared_jpeg_bytes(path: Path) -> bytes:
    """Decode + reorient + downsize to a JPEG buffer CLIP backends can read.

    Immich's own ML container 500s on raw HEIC (it expects the decoded
    preview Immich normally generates server-side, never the original) — so
    every embed goes through this conversion first, mirroring what `process`
    does for the real Y.3 pipeline."""
    import pyvips

    image = pyvips.Image.thumbnail(str(path), 1440)
    return image.write_to_buffer(".jpg[Q=85]")


def _embed_asset(
    conn: sqlite3.Connection,
    asset: AssetLite,
    *,
    backend: str,
    endpoint: str | None,
    model_name: str,
) -> list[float] | None:
    """Get-or-compute one asset's CLIP vector, cached in `embedding` forever
    (per the manifest's docstring: Stage C is the expensive stage, so no
    image is ever embedded twice). Returns None on decode/backend failure —
    caller counts it as a miss and leaves `clip_cos_sim` NULL for a retry."""
    cached = manifest.get_embedding(conn, asset.id, model_name)
    if cached is not None:
        return cached
    import os
    import tempfile

    fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_prepared_jpeg_bytes(Path(asset.path)))
        vec = clip_mod.embed(
            Path(tmp_path), model_name=model_name, backend=backend, endpoint=endpoint
        )
    except Exception:
        return None
    finally:
        os.unlink(tmp_path)
    manifest.set_embedding(conn, asset.id, model_name, vec)
    return vec


def _cosine(u: list[float], v: list[float]) -> float:
    return sum(x * y for x, y in zip(u, v))


def confirm_clip(
    conn: sqlite3.Connection,
    *,
    backend: str,
    endpoint: str | None = None,
    model_name: str = clip_mod.DEFAULT_MODEL,
    progress=None,
) -> dict:
    """Stage C: attach `cluster.clip_cos_sim` to every pending/review cluster
    that doesn't have one yet. Run this after `cluster()` and before (or
    again after) `decide()` — `decide()` only *reads* clip_cos_sim, this is
    the only place that computes and writes it.

    `clip_cos_sim` is min(cosine(winner, member)) over the cluster's image
    members — the whole cluster only counts as CLIP-confirmed if every
    member clears the bar against the winner, not just one lucky pair.
    """
    assets = {a.id: a for a in load_clusterable(conn)}
    cluster_ids = _clip_ready_clusters(conn)
    ok = failed = 0

    for i, cluster_id in enumerate(cluster_ids):
        member_ids = [
            row[0]
            for row in conn.execute(
                "SELECT asset_id FROM membership WHERE cluster_id=?", (cluster_id,)
            )
        ]
        members = [assets[m] for m in member_ids if m in assets]
        images = [m for m in members if m.media_type == "image"]
        if len(images) < 2:
            continue
        winner = max(members, key=winner_score)
        if winner.media_type != "image":
            # Shouldn't happen (video always loses winner_score's format/exif
            # bonuses against any image), but a mixed cluster's cos_sim is
            # meaningless without an image winner to compare against.
            continue

        winner_vec = _embed_asset(conn, winner, backend=backend, endpoint=endpoint, model_name=model_name)
        if winner_vec is None:
            failed += 1
            continue

        sims = []
        member_failed = False
        for member in images:
            if member.id == winner.id:
                continue
            vec = _embed_asset(conn, member, backend=backend, endpoint=endpoint, model_name=model_name)
            if vec is None:
                member_failed = True
                break
            sims.append(_cosine(winner_vec, vec))
        if member_failed or not sims:
            failed += 1
            continue

        conn.execute(
            "UPDATE cluster SET clip_cos_sim=? WHERE id=?", (min(sims), cluster_id)
        )
        ok += 1
        # Commit after every cluster, not batched: each embed call is a
        # network round-trip, and an open write transaction blocks any other
        # writer (e.g. `decide` running concurrently) for its whole span.
        # Committing per-cluster keeps that window to a single cluster's
        # worth of embeds instead of a whole progress-report batch.
        conn.commit()
        if progress and (i + 1) % 25 == 0:
            progress(i + 1, len(cluster_ids))
    if progress:
        progress(len(cluster_ids), len(cluster_ids))
    return {"ok": ok, "failed": failed, "total": len(cluster_ids)}


# -------------------------------------------------------------------- deciding


def winner_score(a: AssetLite) -> float:
    pixels = (a.width or 1) * (a.height or 1)
    return (
        SOURCE_WEIGHT.get(a.source, 50)
        + math.log10(max(pixels, 1)) * 10
        + math.log10(max(a.bytes, 1)) * 5
        + a.exif_fields * 2
        + FORMAT_BONUS.get(a.format, 0)
    )


def _metadata_agrees(a: AssetLite, b: AssetLite) -> bool:
    if a.epoch is not None and b.epoch is not None and abs(a.epoch - b.epoch) <= TIME_BLOCK_SECONDS:
        return True
    if (
        a.gps_lat is not None and b.gps_lat is not None
        and abs(a.gps_lat - b.gps_lat) < GEO_CELL_DEG
        and abs(a.gps_lon - b.gps_lon) < GEO_CELL_DEG
    ):
        return True
    return normalized_stem(a.path) == normalized_stem(b.path)


def _aspect_change(a: AssetLite, b: AssetLite) -> float:
    if not (a.width and a.height and b.width and b.height):
        return 0.0
    ra, rb = a.width / a.height, b.width / b.height
    return abs(ra - rb) / max(ra, rb)


def _is_rotation_swap(a: AssetLite, b: AssetLite, tolerance: float = 0.03) -> bool:
    """Cheap metadata-only pre-check: does `b`'s stored width/height look
    like `a`'s transposed (within `tolerance`)? Diagnosed 2026-07-12:
    3,496 of 4,129 image `aspect-crop` review clusters fit this pattern —
    same photo, orientation-tag disagreement between sources (iCloud vs.
    Google report a rotated capture's dimensions differently), not a real
    crop. This alone is NOT sufficient to bypass the crop guard — a
    genuine crop can coincidentally satisfy it on stored metadata alone
    (Codex review, 2026-07-12); callers must confirm with
    `_confirm_rotation_swap` (decoded, oriented pixels) before trusting it."""
    if not (a.width and a.height and b.width and b.height):
        return False
    return (
        abs(a.width - b.height) / max(a.width, b.height) < tolerance
        and abs(a.height - b.width) / max(a.height, b.width) < tolerance
    )


def _oriented_aspect(path_str: str) -> float | None:
    """Decoded width/height ratio after EXIF autorotation. pHash's own
    hash pipeline force-squashes to a fixed square (see phash.py), which
    is aspect-blind — it can't reliably tell "rotation tag only" apart
    from "genuine crop that happens to have transposed-looking stored
    dimensions". Only real decoded pixels can settle that, cheaply, for
    the handful of candidate pairs `_is_rotation_swap` flags."""
    try:
        import pyvips
        image = pyvips.Image.new_from_file(path_str, access="sequential")
        if hasattr(image, "autorot"):
            image = image.autorot()
        return image.width / image.height
    except Exception:
        return None


def _confirm_rotation_swap(a: AssetLite, b: AssetLite, tolerance: float = 0.03) -> bool:
    """Decode + EXIF-autorotate both files and compare their TRUE oriented
    aspect ratios. `_is_rotation_swap` (stored metadata only) is just a
    cheap pre-filter to avoid decoding every aspect-guard hit; this is the
    actual safety check before letting a transpose pair bypass the crop
    guard — added after Codex flagged that metadata alone can't rule out
    a genuine crop with coincidentally-transposed stored dimensions."""
    aa = _oriented_aspect(a.path)
    ab = _oriented_aspect(b.path)
    if aa is None or ab is None:
        return False
    return abs(aa - ab) / max(aa, ab) < tolerance


def decide(conn: sqlite3.Connection) -> dict:
    """Stage D over every pending/review cluster. Auto-merge on strong pHash
    agreement + metadata, OR (Stage C) a cached CLIP cosine similarity past
    `CLIP_AUTO_THRESHOLD`; everything softer → review.

    Also revisits `review` clusters, not just `pending` ones: `confirm_clip`
    (Stage C) runs after clustering and may attach a fresh `clip_cos_sim` to
    a cluster that was already decided `review` in a prior pass. Re-running
    `_decide_one` is deterministic and idempotent — a cluster with no new
    evidence just reproduces its old decision. Clusters already `auto` are
    excluded by the query and never revisited (their members' asset.status
    has already advanced past `clustered`, so re-admitting them here would
    silently drop members `load_clusterable` no longer returns)."""
    assets = {a.id: a for a in load_clusterable(conn)}
    counts = {"auto": 0, "review": 0, "kept_all": 0}

    for cluster_id, clip_cos in conn.execute(
        "SELECT id, clip_cos_sim FROM cluster WHERE decision IN ('pending', 'review')"
    ).fetchall():
        member_ids = [
            row[0]
            for row in conn.execute(
                "SELECT asset_id FROM membership WHERE cluster_id=?", (cluster_id,)
            )
        ]
        members = [assets[m] for m in member_ids if m in assets]
        if len(members) < 2:
            continue

        decision = _decide_one(members, clip_cos)
        winner = max(members, key=winner_score)
        confidence = _confidence(members, winner)

        conn.execute(
            "UPDATE cluster SET winner_asset_id=?, confidence=?, decision=? WHERE id=?",
            (winner.id, confidence, decision, cluster_id),
        )
        for member in members:
            role = "winner" if member.id == winner.id else (
                "member" if decision == "kept_all" else "loser"
            )
            conn.execute(
                "UPDATE membership SET role=? WHERE asset_id=?", (role, member.id)
            )
            if decision == "auto":
                conn.execute(
                    "UPDATE asset SET status=? WHERE id=? AND status=?",
                    (manifest.DECIDED, member.id, manifest.CLUSTERED),
                )
        counts[decision] += 1
    conn.commit()
    return counts


def _decide_one(members: list[AssetLite], clip_cos: float | None = None) -> str:
    # Guard: burst — shared BurstUUID, or ≥3 shots within 1s at same dims.
    burst_ids = {m.burst_uuid for m in members if m.burst_uuid}
    if burst_ids:
        return "kept_all"
    epochs = sorted(m.epoch for m in members if m.epoch is not None)
    if len(epochs) >= 3 and epochs[-1] - epochs[0] <= 1.0:
        dims = {(m.width, m.height) for m in members}
        if len(dims) == 1:
            return "kept_all"

    # Guard: edited mixed with unedited → human decides.
    if any(m.edited for m in members) and not all(m.edited for m in members):
        return "review"

    winner = max(members, key=winner_score)
    for member in members:
        if member.id == winner.id:
            continue
        # Guard: crop (>5% aspect change vs winner) — but not a plain
        # width/height transpose confirmed by decoded, oriented pixels
        # (an orientation-tag disagreement between sources, not a crop;
        # see _is_rotation_swap / _confirm_rotation_swap).
        if _aspect_change(winner, member) > 0.05 and not (
            _is_rotation_swap(winner, member) and _confirm_rotation_swap(winner, member)
        ):
            return "review"
        # Displacing a library/originals file is a swap, not a promote.
        if member.source == "originals":
            return "review"
        # Strong visual evidence + one agreeing metadata signal, per pair.
        if member.media_type == "image":
            if (
                winner.phash is None or member.phash is None
                or phash.hamming(winner.phash, member.phash) > HAMMING_STRONG
            ):
                # Stage C: a cached CLIP cosine past the calibrated bar
                # substitutes for the missing strong-pHash agreement. Below
                # the bar (or not yet computed) falls through to review,
                # same as pre-Stage-C behavior.
                if clip_cos is None or clip_cos < CLIP_AUTO_THRESHOLD:
                    return "review"
        else:
            if member.bytes != winner.bytes:
                return "review"
        if not _metadata_agrees(winner, member):
            return "review"
    return "auto"


def _confidence(members: list[AssetLite], winner: AssetLite) -> float:
    distances = [
        phash.hamming(winner.phash, m.phash)
        for m in members
        if m.id != winner.id and m.phash is not None and winner.phash is not None
    ]
    if not distances:
        return 0.5
    return 1.0 - max(distances) / 64.0


# --------------------------------------------------------------------- apply


def _promote_dest(originals_root: Path, path_str: str, taken_at: str | None) -> Path:
    name = Path(path_str).name
    if taken_at:
        try:
            dt = datetime.fromisoformat(taken_at)
            return originals_root / f"{dt.year:04d}" / f"{dt.month:02d}" / name
        except ValueError:
            pass
    return originals_root / "unknown-date" / name


def _quarantine_dest(quarantine_root: Path, path_str: str) -> Path:
    """Mirror the staging tree 1:1 under quarantine_root — every loser stays
    traceable back to exactly where it came from. The fallback (a path
    outside /staging, which shouldn't happen in practice) is basename-only
    and CAN collide, so callers must still run it through the same
    collision handling as promote dests — never assume this is collision-free."""
    p = Path(path_str)
    try:
        rel = p.relative_to("/staging")
    except ValueError:
        rel = Path(p.name)
    return quarantine_root / rel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_dest(dest: Path, asset_id: int, expected_bytes: int | None) -> tuple[Path, bool]:
    """Decide the real destination for one asset, handling every crash-
    recovery case up front so `_safe_move` never has to guess:

    - `dest` free                              -> use it, nothing to skip.
    - `dest` occupied by THIS asset already
      (size matches what the manifest recorded) -> a prior run got as far
      as the copy (or further) before dying; tell the caller to skip the
      copy and just finish bookkeeping.
    - `dest` occupied by a DIFFERENT asset      -> genuine collision (two
      distinct source files sharing a name); move to a deterministic,
      asset-id-qualified name instead. That name is astronomically
      unlikely to itself collide, but if THIS asset was itself the one
      interrupted at that qualified name, the same "already there" check
      applies again.

    Returns (final_dest, already_done).
    """
    if not dest.exists():
        return dest, False
    if expected_bytes is not None and dest.stat().st_size == expected_bytes:
        return dest, True
    qualified = dest.with_name(f"{dest.stem}__{asset_id}{dest.suffix}")
    if not qualified.exists():
        return qualified, False
    if expected_bytes is not None and qualified.stat().st_size == expected_bytes:
        return qualified, True
    # Both the plain and asset-id-qualified names are taken by something
    # else entirely — vanishingly unlikely, but refuse to guess further.
    raise FileExistsError(f"dest collision unresolved for asset {asset_id}: {dest}")


def _safe_move(src: Path, dst: Path) -> None:
    """Copy-hash-verify-delete, never a bare rename or blind overwrite.
    Staging and originals/quarantine live on different ZFS datasets
    (cross-device rename fails), so this is copy+verify+delete — and the
    verify is a full sha256, not just a size check, because a same-size
    silent-corruption copy over NFS/a bind mount is exactly the failure
    mode that must never lead to deleting the only good copy.

    `dst` must not already exist — callers resolve collisions via
    `_resolve_dest` first; this function only ever writes fresh files, so
    it never has to decide whether an existing `dst` is safe to clobber.
    """
    if dst.exists():
        raise FileExistsError(f"refusing to overwrite existing dest: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.partial")
    hasher = hashlib.sha256()
    with open(src, "rb") as fsrc, open(tmp, "wb") as ftmp:
        for chunk in iter(lambda: fsrc.read(4 * 1024 * 1024), b""):
            ftmp.write(chunk)
            hasher.update(chunk)
    src_hash, src_size = hasher.hexdigest(), src.stat().st_size
    if tmp.stat().st_size != src_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"copy size mismatch: {src} -> {dst}")
    shutil.copystat(src, tmp)
    tmp.rename(dst)
    if _sha256(dst) != src_hash:
        # dst is corrupt — remove it so a re-run doesn't mistake it for a
        # completed move and skip re-copying a good source that's still here.
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"post-move hash mismatch: {src} -> {dst}")
    src.unlink()


def _rescue_sidecar(dest: Path, taken_at: str | None, gps_lat: float | None, gps_lon: float | None) -> bool:
    """Google Takeout strips or mangles EXIF; `fingerprint_fields` already
    rescued the real date/GPS from the `*.json` companion into the manifest
    (taken_src='json') — write it back out as an XMP sidecar next to the
    promoted file so Immich actually sees it, instead of an undated/
    un-geotagged asset."""
    patch: dict[str, object] = {}
    if taken_at:
        try:
            dt = datetime.fromisoformat(taken_at)
            patch["DateTimeOriginal"] = dt.strftime("%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    if gps_lat is not None and gps_lon is not None:
        patch["GPSLatitude"] = abs(gps_lat)
        patch["GPSLatitudeRef"] = "N" if gps_lat >= 0 else "S"
        patch["GPSLongitude"] = abs(gps_lon)
        patch["GPSLongitudeRef"] = "E" if gps_lon >= 0 else "W"
    if not patch:
        return False
    sidecar.write(dest, patch)
    return True


def apply_decisions(
    conn: sqlite3.Connection,
    *,
    originals_root: Path,
    quarantine_root: Path,
    dry_run: bool = True,
    limit: int | None = None,
    progress=None,
) -> dict:
    """Stage E: the step nothing before this has ever executed. `decide`
    only writes `decision`/`winner_asset_id` to the manifest — this is what
    actually acts on an `auto` cluster: promote the winner into
    `originals_root` (skipped if it's already a canonical/`originals`
    asset — it's already in Immich), quarantine every loser into
    `quarantine_root` (mirroring its staging path, never deleted here —
    purging is a separate, later command).

    Idempotent and crash-safe: only `decided` assets belonging to `auto`
    clusters are selected, `_resolve_dest` recognizes a destination a prior
    run already finished writing (by size match) so a kill at ANY point —
    mid-copy, between rename and unlink, or between unlink and commit — is
    safe to resume, and status commits after every single asset (not
    batched) so the manifest is never more than one file out of sync with
    disk.
    """
    rows = conn.execute(
        """SELECT a.id, a.source, a.path, a.bytes, a.taken_at, a.taken_src,
                  a.gps_lat, a.gps_lon, c.winner_asset_id
           FROM asset a
           JOIN membership m ON m.asset_id = a.id
           JOIN cluster c ON c.id = m.cluster_id
           WHERE a.status = ? AND c.decision = 'auto'
           ORDER BY a.id""",
        (manifest.DECIDED,),
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]

    counts = {
        "promoted": 0, "quarantined": 0,
        "promoted_bytes": 0, "quarantined_bytes": 0,
        "sidecars_written": 0, "errors": 0,
    }
    errors: list[str] = []
    total = len(rows)

    for i, (asset_id, source, path_str, nbytes, taken_at, taken_src, gps_lat, gps_lon, winner_id) in enumerate(rows):
        src = Path(path_str)
        is_winner = asset_id == winner_id
        try:
            base_dest = (
                _promote_dest(originals_root, path_str, taken_at) if is_winner
                else _quarantine_dest(quarantine_root, path_str)
            )
            if not dry_run:
                dest, already_done = _resolve_dest(base_dest, asset_id, nbytes)
                if not already_done:
                    if not src.exists():
                        # Neither the source nor a matching dest exists —
                        # genuinely missing, not a resumable state. Surface
                        # loudly rather than silently marking it done.
                        raise FileNotFoundError(str(src))
                    _safe_move(src, dest)
                elif src.exists():
                    # Copy finished in a prior run but the source unlink
                    # (or the status commit) never happened — finish it.
                    src.unlink()
                if is_winner and source == "google" and taken_src == "json":
                    if _rescue_sidecar(dest, taken_at, gps_lat, gps_lon):
                        counts["sidecars_written"] += 1
                conn.execute(
                    "UPDATE asset SET status=? WHERE id=?",
                    (manifest.PROMOTED if is_winner else manifest.QUARANTINED, asset_id),
                )
                conn.commit()
            if is_winner:
                counts["promoted"] += 1
                counts["promoted_bytes"] += nbytes or 0
            else:
                counts["quarantined"] += 1
                counts["quarantined_bytes"] += nbytes or 0
        except Exception as exc:  # noqa: BLE001 — one bad file must not kill the batch
            counts["errors"] += 1
            errors.append(f"{path_str}: {exc}")

        if progress:
            progress(i + 1, total)

    counts["error_samples"] = errors[:20]
    return counts
