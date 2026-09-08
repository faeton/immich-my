"""`immy similar` — image-to-image search against Immich's CLIP index.

Immich's UI only offers text→image smart search; there's no "find shots like
this photo". But the index is right there: `smart_search(assetId, embedding)`
with a cosine vchordrq index. Embed the query image with Immich's *own* ONNX
model (or the NAS ML server — same vector space) and ask pgvector for the
nearest neighbours. Read-only; nothing is written.

Score guide (cosine similarity, ViT-B-32), calibrated 2026-09-08 on the real
library: a 343-px q65 re-compression of a library HEIC scored 0.989 against
itself, while selfies of the same person from *different years* all landed
0.92–0.94. So:
  >= 0.95  same frame (another export/crop/re-compression of it)
  0.85–0.95 same subject — same person/pose/kind of shot, NOT the same photo
  < 0.85    merely the same kind of picture
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from .clip import to_pgvector_literal

SAME_FRAME = 0.95
SAME_SUBJECT = 0.85


@dataclass(frozen=True)
class Hit:
    asset_id: str
    original_path: str
    original_file_name: str
    asset_type: str
    taken_at: datetime | None
    city: str | None
    country: str | None
    similarity: float

    @property
    def label(self) -> str:
        return label_for(self.similarity)


def label_for(similarity: float) -> str:
    if similarity >= SAME_FRAME:
        return "same frame"
    if similarity >= SAME_SUBJECT:
        return "same subject"
    return "similar"


_SQL = """
SELECT a.id, a."originalPath", a."originalFileName", a.type, a."localDateTime",
       e.city, e.country,
       1 - (s.embedding <=> %(v)s::vector) AS similarity
FROM smart_search s
JOIN asset a ON a.id = s."assetId"
LEFT JOIN asset_exif e ON e."assetId" = a.id
WHERE a."deletedAt" IS NULL
  AND (%(videos)s OR a.type = 'IMAGE')
ORDER BY s.embedding <=> %(v)s::vector
LIMIT %(limit)s
"""


def search(
    conn: psycopg.Connection,
    embedding: list[float],
    *,
    limit: int = 30,
    include_videos: bool = True,
    min_similarity: float = 0.0,
) -> list[Hit]:
    """Nearest neighbours of `embedding` in `smart_search`, best first.

    `min_similarity` filters client-side so the ANN index still gets a plain
    `ORDER BY … LIMIT` (a WHERE on the distance would defeat it).
    """
    rows = conn.execute(
        _SQL,
        {"v": to_pgvector_literal(embedding), "videos": include_videos, "limit": limit},
    ).fetchall()
    hits = [
        Hit(
            asset_id=str(r[0]), original_path=r[1], original_file_name=r[2],
            asset_type=r[3], taken_at=r[4], city=r[5], country=r[6],
            similarity=float(r[7]),
        )
        for r in rows
    ]
    return [h for h in hits if h.similarity >= min_similarity]


def coverage(conn: psycopg.Connection) -> tuple[int, int]:
    """(assets with a CLIP vector, live assets) — how much of the library the
    search can even see. immy-inserted assets never auto-queue SmartSearch."""
    embedded = conn.execute(
        'SELECT count(*) FROM smart_search s JOIN asset a ON a.id = s."assetId" '
        'WHERE a."deletedAt" IS NULL'
    ).fetchone()[0]
    live = conn.execute('SELECT count(*) FROM asset WHERE "deletedAt" IS NULL').fetchone()[0]
    return int(embedded), int(live)


__all__ = ["Hit", "SAME_FRAME", "SAME_SUBJECT", "label_for", "search", "coverage"]
