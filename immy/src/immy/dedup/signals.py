"""Pixel-identity signals for the residual review queue (`dedup rescore`).

The clusters that survive human sweeps of the high-CLIP band are ~75%
"pHash weak": CLIP says same scene, pHash says different pixels. That
population is two opposite truths mixed together:

  A. true duplicates whose re-encode/resize/crop broke pHash  -> merge
  B. distinct shots of the same scene, seconds apart          -> keep all

CLIP cannot separate A from B — scene embeddings score both ~0.95. But a
plain normalized cross-correlation (NCC) on small grayscale renders can:
a re-export of the SAME frame correlates near 1.0 even through heavy
recompression, while a burst neighbour where anything moved drops well
below. This module computes that signal once per cluster into a side
table (`review_signal`) the review tool reads for display and sweeps.

Decision-support only: nothing here writes decisions or touches the
pipeline's own tables. The table is keyed by cluster and cheap to
recompute (sources are the review tool's cached 640px thumbnails).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .engine import AssetLite, load_cluster_members

_CREATE = """
CREATE TABLE IF NOT EXISTS review_signal (
  cluster_id  INTEGER PRIMARY KEY REFERENCES cluster(id),
  pixel_ncc   REAL,   -- min over (winner, member) pairs; NULL if undecodable
  time_delta  REAL    -- min |taken_at delta| seconds over reliably-timed pairs
);
"""

# Reliable capture-time provenance (mirrors engine's video reliability rule:
# mtime reflects copy time, filename is a guess).
_RELIABLE_TAKEN = ("exif", "json", "companion")

NCC_EDGE = 128   # comparison raster; identity survives far below this


def _gray(path: Path, edge: int = NCC_EDGE):
    """Decode to a edge×edge grayscale float array (aspect deliberately
    ignored — a stretch hits both images of a pair identically)."""
    import numpy as np
    import pyvips

    img = pyvips.Image.thumbnail(str(path), edge, height=edge, size="force")
    img = img.colourspace("b-w")
    data = np.frombuffer(img.write_to_memory(), dtype=np.uint8)
    return data.reshape(img.height, img.width, img.bands)[:, :, 0].astype(np.float64)


def ncc(a, b) -> float | None:
    """Normalized cross-correlation in [-1, 1]; None for degenerate images."""
    a = a - a.mean()
    b = b - b.mean()
    denom = ((a * a).sum() * (b * b).sum()) ** 0.5
    if denom == 0:
        return None
    return float((a * b).sum() / denom)


def _epoch(m: AssetLite) -> float | None:
    if m.taken_src not in _RELIABLE_TAKEN or not m.taken_at:
        return None
    try:
        return datetime.fromisoformat(m.taken_at).timestamp()
    except ValueError:
        return None


def compute_signals(
    conn: sqlite3.Connection,
    thumb_root: Path,
    *,
    progress=None,
    force: bool = False,
) -> dict:
    """Fill `review_signal` for every review cluster with a CLIP score.

    Reads the review tool's cached 640px thumbnails (generating any that
    are missing via review.make_thumb), so a fully-warmed cache makes this
    minutes, not hours. Idempotent; `force` recomputes existing rows.
    """
    from .review import THUMB_SIZE, default_winner, make_thumb

    conn.executescript(_CREATE)
    cluster_ids = [
        r[0] for r in conn.execute(
            "SELECT c.id FROM cluster c"
            " LEFT JOIN review_signal s ON s.cluster_id = c.id"
            " WHERE c.decision='review' AND c.clip_cos_sim IS NOT NULL"
            + ("" if force else " AND s.cluster_id IS NULL")
            + " ORDER BY c.id"
        )
    ]
    gray_cache: dict[int, object] = {}

    def member_gray(m: AssetLite):
        if m.id in gray_cache:
            return gray_cache[m.id]
        thumb = thumb_root / str(THUMB_SIZE) / f"{m.id}.jpg"
        if not thumb.exists():
            thumb.parent.mkdir(parents=True, exist_ok=True)
            if not make_thumb(m.path, thumb, THUMB_SIZE):
                gray_cache[m.id] = None
                return None
        try:
            gray_cache[m.id] = _gray(thumb)
        except Exception:
            gray_cache[m.id] = None
        return gray_cache[m.id]

    ok = failed = 0
    for i, cluster_id in enumerate(cluster_ids):
        members = load_cluster_members(conn, cluster_id)
        images = [m for m in members if m.media_type == "image"]
        if len(images) < 2:
            continue
        winner = default_winner(members)
        if winner.media_type != "image":
            continue

        winner_gray = member_gray(winner)
        nccs: list[float] = []
        deltas: list[float] = []
        winner_epoch = _epoch(winner)
        for m in images:
            if m.id == winner.id:
                continue
            if winner_gray is not None:
                other = member_gray(m)
                if other is not None:
                    value = ncc(winner_gray, other)
                    if value is not None:
                        nccs.append(value)
            m_epoch = _epoch(m)
            if winner_epoch is not None and m_epoch is not None:
                deltas.append(abs(winner_epoch - m_epoch))

        pixel = min(nccs) if nccs else None
        delta = min(deltas) if deltas else None
        conn.execute(
            "INSERT INTO review_signal (cluster_id, pixel_ncc, time_delta)"
            " VALUES (?, ?, ?) ON CONFLICT(cluster_id) DO UPDATE SET"
            " pixel_ncc=excluded.pixel_ncc, time_delta=excluded.time_delta",
            (cluster_id, pixel, delta),
        )
        ok += 1 if pixel is not None else 0
        failed += 1 if pixel is None else 0
        if (i + 1) % 100 == 0:
            conn.commit()
            if progress:
                progress(i + 1, len(cluster_ids))
    conn.commit()
    if progress:
        progress(len(cluster_ids), len(cluster_ids))
    return {"scored": ok, "no_pixels": failed, "total": len(cluster_ids)}


def get_signal(conn: sqlite3.Connection, cluster_id: int) -> tuple[float | None, float | None]:
    """(pixel_ncc, time_delta) or (None, None) — table may not exist yet."""
    try:
        row = conn.execute(
            "SELECT pixel_ncc, time_delta FROM review_signal WHERE cluster_id=?",
            (cluster_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return (None, None)
    return (row[0], row[1]) if row else (None, None)
