"""Triage scan/report engine.

Design mirrors `dedup.engine`: the manifest is the durable ledger, every
expensive artifact (probe result, sampled frames, CLIP vector) is cached so
re-runs only pay for new clips, and the cheap derived layers (take-grouping,
suggestions) are recomputed from scratch on every scan so a rule tweak never
needs a --force.

Path model — one clip, three anchors:
  manifest  /originals/<trip>/DJI_0001.MP4     (asset.path, container view)
  filesystem <fs_root>/<trip>/DJI_0001.MP4     (where ffmpeg reads it now)
  immich    <import_root>/<trip>/DJI_0001.MP4  (asset."originalPath" in PG)
`fs_root` defaults to the manifest root (inside the deploy/n5 container they
coincide); host-side runs pass --fs-root /mnt/tank/immich/originals. The
immich anchor comes from the library's importPaths[0] via pg.fetch_library_info,
same as `process`.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from .. import clip as clip_mod
from ..dedup import manifest as manifest_mod
from ..video import VideoProbeError, probe

# Formats treated as video when `asset.media_type` was never fingerprinted.
VIDEO_FORMATS = {
    "mp4", "mov", "m4v", "avi", "mkv", "mts", "webm", "wmv",
    "mpg", "mpeg", "3gp", "insv", "360",
}

# Camera low-res proxy sidecars — derivable from their master, excluded from
# the vv mirror, and their fate follows the master's verdict, so they are
# OUT of triage scope entirely (grading one is a wasted human decision).
PROXY_SUFFIXES = {".lrv", ".lrf"}


def is_proxy(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix.lower() in PROXY_SUFFIXES:
        return True
    # Insta360 marks its stitched low-res preview by PREFIX, not extension:
    # LRV_<ts>_11_<serial>.insv next to the VID_ _00_/_10_ lens masters
    # (see insta360.py — lens code 11 = both hemispheres combined).
    return p.name.lower().startswith("lrv_") and p.suffix.lower() == ".insv"

FRAMES_PER_CLIP = 6
TAKE_GAP_S = 120.0        # capture-time gap that always starts a new take
TAKE_MIN_COS = 0.80       # centroid cosine below this starts a new take
COMPRESS_MIN_DURATION_S = 120.0
COMPRESS_MIN_KBPS = 40_000.0
REVIEW_TAKE_MIN_CLIPS = 3

_DATED_TOP = re.compile(r"^\d{4}$")


# ------------------------------------------------------------------ paths


def trip_of(path: str, root: str) -> str | None:
    """Trip-dir name for a manifest path, or None when the clip lives in the
    dated YYYY/ tree (cloud imports — out of triage scope) or outside root."""
    root = root.rstrip("/")
    if not path.startswith(root + "/"):
        return None
    top = PurePosixPath(path[len(root) + 1:]).parts
    if not top:
        return None
    head = top[0]
    if _DATED_TOP.match(head) or head.startswith("_") or head.startswith("."):
        return None
    return head


def map_path(path: str, root: str, fs_root: str) -> Path:
    root = root.rstrip("/")
    fs_root = fs_root.rstrip("/")
    if root == fs_root or not path.startswith(root + "/"):
        return Path(path)
    return Path(fs_root + path[len(root):])


# ------------------------------------------------------------------ rows


@dataclass
class Clip:
    """One trip video: manifest projection + everything scan learns."""

    id: int
    path: str
    trip: str
    bytes: int
    mtime: float | None
    taken_at: str | None
    duration_s: float | None = None
    codec: str | None = None
    bitrate_kbps: float | None = None
    favorite: int | None = None   # None = Immich didn't answer
    album_count: int | None = None
    take_group: int | None = None
    vec: list[float] | None = None

    def sort_epoch(self) -> float:
        if self.taken_at:
            try:
                return datetime.fromisoformat(self.taken_at).timestamp()
            except ValueError:
                pass
        return self.mtime or 0.0


def load_trip_videos(conn, root: str) -> list[Clip]:
    fmt_list = ",".join(f"'{f}'" for f in sorted(VIDEO_FORMATS))
    rows = conn.execute(
        "SELECT id, path, bytes, mtime, taken_at FROM asset "
        "WHERE source='originals' AND (media_type='video' "
        f"  OR (media_type IS NULL AND format IN ({fmt_list}))) "
        "ORDER BY path"
    ).fetchall()
    clips = []
    for id_, path, bytes_, mtime, taken_at in rows:
        trip = trip_of(path, root)
        if trip is None or is_proxy(path):
            continue
        clips.append(Clip(
            id=id_, path=path, trip=trip, bytes=bytes_ or 0,
            mtime=mtime, taken_at=taken_at,
        ))
    return clips


# ------------------------------------------------------------------ frames


def _extract_frame(src: Path, dst: Path, seek_s: float) -> None:
    """One small JPEG at `seek_s` — extract_poster's fast-seek recipe, plus a
    640px downscale (CLIP resizes to 224 anyway; keeps scratch small)."""
    if shutil.which("ffmpeg") is None:
        raise VideoProbeError("ffmpeg not on PATH — install ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(seek_s, 0.0):.3f}", "-i", str(src),
        "-vf", "scale=640:-2", "-frames:v", "1", "-q:v", "3",
        "-y", str(dst),
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise VideoProbeError(
            f"frame extract failed on {src.name}@{seek_s:.1f}s: "
            f"{(proc.stderr or '').strip()[:200]}"
        )


def sample_frames(
    src: Path, out_dir: Path, duration_s: float | None,
    *, extract_fn: Callable[[Path, Path, float], None] = _extract_frame,
) -> list[str]:
    """FRAMES_PER_CLIP evenly spaced frames → `<out_dir>/f<i>.jpg`.

    Timestamps sit mid-slot ((i+0.5)/n) so a 6-frame sample of a 60 s clip
    reads 5 s..55 s — never the black lead-in or the last GOP. Existing
    files are trusted (idempotent re-runs)."""
    n = FRAMES_PER_CLIP
    if not duration_s or duration_s <= 0 or math.isnan(duration_s):
        stamps = [0.0]
    else:
        stamps = [duration_s * (i + 0.5) / n for i in range(n)]
    names = []
    for i, ts in enumerate(stamps):
        dst = out_dir / f"f{i}.jpg"
        if not dst.exists():
            extract_fn(src, dst, ts)
        names.append(dst.name)
    return names


def pool_frames(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Mean-pool frame vectors into one clip vector, L2-normalized so cosine
    against other clip vectors behaves like the image-image case."""
    if not vectors:
        raise ValueError("no vectors to pool")
    dim = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in mean)) or 1.0
    return [x / norm for x in mean]


def _cosine(u: Sequence[float], v: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(u, v))


# ------------------------------------------------------------------ takes


def assign_take_groups(
    clips: list[Clip],
    *,
    gap_s: float = TAKE_GAP_S,
    min_cos: float = TAKE_MIN_COS,
    start_group: int = 1,
) -> int:
    """Assign `take_group` in place; returns the next unused group id.

    Clips are grouped per trip, in capture order. A new group starts on a
    capture gap > `gap_s`, or when the clip's CLIP vector strays below
    `min_cos` cosine from the running (unnormalized-mean) group centroid.
    Missing vectors never split a group — the time gap alone decides, so
    an embed outage degrades to time-only grouping instead of shattering
    every take into singletons."""
    group = start_group
    by_trip: dict[str, list[Clip]] = {}
    for c in clips:
        by_trip.setdefault(c.trip, []).append(c)

    for trip in sorted(by_trip):
        members = sorted(by_trip[trip], key=lambda c: (c.sort_epoch(), c.id))
        prev_epoch: float | None = None
        centroid_sum: list[float] | None = None
        centroid_n = 0
        for c in members:
            epoch = c.sort_epoch()
            split = prev_epoch is not None and (epoch - prev_epoch) > gap_s
            if not split and c.vec is not None and centroid_sum is not None:
                centroid = pool_frames([[x / centroid_n for x in centroid_sum]])
                if _cosine(c.vec, centroid) < min_cos:
                    split = True
            if split or prev_epoch is None:
                if prev_epoch is not None:
                    group += 1
                centroid_sum, centroid_n = None, 0
            c.take_group = group
            if c.vec is not None:
                if centroid_sum is None:
                    centroid_sum = list(c.vec)
                    centroid_n = 1
                else:
                    centroid_sum = [a + b for a, b in zip(centroid_sum, c.vec)]
                    centroid_n += 1
            prev_epoch = epoch
        group += 1
    return group


# ------------------------------------------------------------------ suggest


def suggest(clip: Clip, take_size: int) -> tuple[str | None, str | None]:
    """Conservative auto-suggestion — advisory only, never a verdict.

    Precedence: an Immich favorite always suggests keep (a human already
    voted with their thumb). Album membership is deliberately NOT a keep
    signal — immy's auto-albums put every trip clip in an album, so the
    first scan of n5 marked 1.86 TB "keep" off albums alone. Long
    high-bitrate non-favorites are compress candidates; ≥3-clip takes are
    flagged for human take-review."""
    if bool(clip.favorite):
        return "keep", "immich favorite"
    if (
        (clip.duration_s or 0) > COMPRESS_MIN_DURATION_S
        and (clip.bitrate_kbps or 0) > COMPRESS_MIN_KBPS
        # .insv is never a compress candidate: a transcode strips the
        # gyro/stitch metadata and Insta360 Studio can't open the result.
        and not clip.path.lower().endswith(".insv")
    ):
        return "compress", (
            f"{clip.duration_s:.0f}s at {clip.bitrate_kbps / 1000:.0f} Mbps"
        )
    if take_size >= REVIEW_TAKE_MIN_CLIPS:
        return "review-take", f"{take_size}-clip take group"
    return None, None


# ------------------------------------------------------------------ scan


def scan(
    conn,
    *,
    root: str = "/originals",
    fs_root: str | None = None,
    frames_root: Path,
    backend: str,
    endpoint: str | None,
    model_name: str,
    immich_lookup: Callable[[list[str]], dict[str, tuple[bool, int]]] | None = None,
    probe_fn: Callable[[Path], object] = probe,
    extract_fn: Callable[[Path, Path, float], None] = _extract_frame,
    embed_fn: Callable[..., list[float]] | None = None,
    force: bool = False,
    limit: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] = lambda s: None,
) -> dict:
    """Gather signals for every trip video into `video_signal`.

    Per-clip work (probe, frames, CLIP) is cached and resumable — commit
    per clip, ^C-safe. Derived layers (Immich flags, take-grouping,
    suggestions) are recomputed over the full clip set every run."""
    fs_root = fs_root or root
    embed_fn = embed_fn or clip_mod.embed
    # Self-heal a scope widening/narrowing: signals for now-out-of-scope
    # proxies are scan-derived data, safe to drop (v1 scanned .lrv/.lrf).
    conn.execute(
        "DELETE FROM video_signal WHERE asset_id IN ("
        "  SELECT id FROM asset WHERE LOWER(path) LIKE '%.lrv'"
        "    OR LOWER(path) LIKE '%.lrf'"
        r"    OR LOWER(path) LIKE '%/lrv\_%.insv' ESCAPE '\')"
    )
    conn.commit()
    clips = load_trip_videos(conn, root)

    done = {
        row[0] for row in conn.execute("SELECT asset_id FROM video_signal")
    }
    todo = [c for c in clips if force or c.id not in done]
    if limit:
        todo = todo[:limit]

    ok = failed = 0
    for i, c in enumerate(todo):
        if progress:
            progress(i + 1, len(todo))
        src = map_path(c.path, root, fs_root)
        try:
            info = probe_fn(src)
            c.duration_s = getattr(info, "duration_s", None)
            c.codec = getattr(info, "video_codec", None)
            if c.duration_s and c.duration_s > 0:
                c.bitrate_kbps = c.bytes * 8 / c.duration_s / 1000.0
            frame_dir = frames_root / str(c.id)
            names = sample_frames(
                src, frame_dir, c.duration_s, extract_fn=extract_fn
            )
            vec = manifest_mod.get_embedding(conn, c.id, model_name)
            if vec is None:
                frame_vecs = [
                    embed_fn(
                        frame_dir / n, model_name=model_name,
                        backend=backend, endpoint=endpoint,
                    )
                    for n in names
                ]
                vec = pool_frames(frame_vecs)
                manifest_mod.set_embedding(conn, c.id, model_name, vec)
            c.vec = vec
            conn.execute(
                "INSERT INTO video_signal (asset_id, duration_s, codec, "
                "  bitrate_kbps, frames_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET duration_s=excluded.duration_s, "
                "  codec=excluded.codec, bitrate_kbps=excluded.bitrate_kbps, "
                "  frames_json=excluded.frames_json",
                (c.id, c.duration_s, c.codec, c.bitrate_kbps,
                 json.dumps([f"{c.id}/{n}" for n in names])),
            )
            conn.commit()
            ok += 1
        except Exception as e:  # probe/extract/embed — skip, retry next run
            failed += 1
            log(f"skip {src.name}: {e}")
            continue

    # hydrate already-scanned clips (needed for grouping across the full set)
    sig = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT asset_id, duration_s, codec, bitrate_kbps FROM video_signal"
        )
    }
    scanned = [c for c in clips if c.id in sig]
    for c in scanned:
        c.duration_s, c.codec, c.bitrate_kbps = sig[c.id]
        if c.vec is None:
            c.vec = manifest_mod.get_embedding(conn, c.id, model_name)

    # Immich favorite/album flags (best-effort — a down DB degrades to NULLs)
    flagged = 0
    if immich_lookup is not None and scanned:
        flags = immich_lookup([c.path for c in scanned])
        for c in scanned:
            if c.path in flags:
                c.favorite, c.album_count = (
                    int(flags[c.path][0]), int(flags[c.path][1]),
                )
                flagged += 1

    assign_take_groups(scanned)
    take_sizes: dict[int, int] = {}
    for c in scanned:
        take_sizes[c.take_group] = take_sizes.get(c.take_group, 0) + 1

    for c in scanned:
        suggested, reason = suggest(c, take_sizes.get(c.take_group, 1))
        conn.execute(
            "UPDATE video_signal SET take_group=?, favorite=?, album_count=?, "
            "  suggested=?, suggest_reason=? WHERE asset_id=?",
            (c.take_group, c.favorite, c.album_count, suggested, reason, c.id),
        )
    conn.commit()

    return {
        "eligible": len(clips), "scanned_now": ok, "failed": failed,
        "total_scanned": len(scanned), "immich_flagged": flagged,
        "take_groups": len(take_sizes),
    }


# ------------------------------------------------------------------ report


def report(conn, *, root: str = "/originals") -> dict:
    """Per-trip rollup of scanned clips, sorted by recoverable GB."""
    rows = conn.execute(
        "SELECT a.path, a.bytes, s.take_group, s.suggested, s.favorite "
        "FROM asset a JOIN video_signal s ON s.asset_id = a.id"
    ).fetchall()
    take_sizes: dict[int, int] = {}
    for _, _, tg, _, _ in rows:
        if tg is not None:
            take_sizes[tg] = take_sizes.get(tg, 0) + 1

    trips: dict[str, dict] = {}
    for path, bytes_, tg, suggested, favorite in rows:
        trip = trip_of(path, root)
        if trip is None or is_proxy(path):
            continue
        t = trips.setdefault(trip, {
            "clips": 0, "bytes": 0, "take_bytes": 0,
            "compress_bytes": 0, "favorites": 0,
        })
        t["clips"] += 1
        t["bytes"] += bytes_ or 0
        if tg is not None and take_sizes.get(tg, 0) >= REVIEW_TAKE_MIN_CLIPS:
            t["take_bytes"] += bytes_ or 0
        if suggested == "compress":
            t["compress_bytes"] += bytes_ or 0
        if favorite:
            t["favorites"] += 1

    ordered = dict(
        sorted(trips.items(), key=lambda kv: kv[1]["bytes"], reverse=True)
    )
    totals = {
        k: sum(t[k] for t in ordered.values())
        for k in ("clips", "bytes", "take_bytes", "compress_bytes", "favorites")
    }
    return {"trips": ordered, "totals": totals}
