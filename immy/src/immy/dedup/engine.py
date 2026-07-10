"""Dedup cascade engine — Stages A/B/D over the manifest.

Implements the locked cascade from raw/CONSOLIDATION-PIPELINE.md:

    Stage A  block      EXIF time ±3s | day + ~100m GPS cell | filename stem
    Stage B  prefilter  pHash Hamming ≤10 candidate, ≤6 strong
    Stage C  confirm    CLIP cosine — NOT wired yet; needs threshold
                        calibration on a labeled sample first. Clusters that
                        would need CLIP to decide land in `review` instead of
                        being guessed at.
    Stage D  decide     auto-merge only on strong pHash + ≥1 agreeing
                        metadata signal, and never past the guard rails
                        (burst / Live pair / edited / aspect change).

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

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path

from . import manifest, phash
from .. import dates
from ..exif import ExifRow

HAMMING_CANDIDATE = 10
HAMMING_STRONG = 6

TIME_BLOCK_SECONDS = 3
GEO_CELL_DEG = 0.001          # ~100 m at mid latitudes
MAX_BLOCK = 200               # bail-out for degenerate blocks

VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "mts", "m2ts", "insv", "lrv", "lrf"}

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


def _pair_evidence(a: AssetLite, b: AssetLite) -> tuple[str, int | None] | None:
    """Stage B verdict for one candidate pair.

    Returns (tier, hamming) — tier 'strong'|'candidate' — or None (not a
    dupe pair)."""
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
        # v1: no frame decode for videos. Byte-identical size + agreeing
        # timestamps = strong; a mere shared block = candidate (review).
        if a.bytes and a.bytes == b.bytes:
            return ("strong", None)
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


def decide(conn: sqlite3.Connection) -> dict:
    """Stage D over every pending cluster. CLIP-less v1: auto-merge only
    on strong pHash agreement + metadata; everything softer → review."""
    assets = {a.id: a for a in load_clusterable(conn)}
    counts = {"auto": 0, "review": 0, "kept_all": 0}

    for (cluster_id,) in conn.execute(
        "SELECT id FROM cluster WHERE decision='pending'"
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

        decision = _decide_one(members)
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


def _decide_one(members: list[AssetLite]) -> str:
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
        # Guard: crop (>5% aspect change vs winner).
        if _aspect_change(winner, member) > 0.05:
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
                return "review"  # CLIP territory — Stage C, once calibrated
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
