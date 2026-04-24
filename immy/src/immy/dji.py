"""DJI master/proxy pairing.

DJI drones (Mavic, Mini, Air, Avata…) record each clip as two files
sharing a stem, differing only in extension:
  - `DJI_<timestamp>_<idx>_<suffix>.MP4` — the full-quality master
    (H.264/H.265, 4K/5.4K/6K, tens to hundreds of MB per minute).
  - `DJI_<timestamp>_<idx>_<suffix>.LRF` — DJI's own low-res proxy
    ("Low Resolution Footage"), ~1/10th the size, H.264 in an MP4-ish
    container. Produced in-camera for editor scrubbing.

Like Insta360's LRV, the LRF is perfect as a `derivative_source`: we
point ffmpeg at it to build the MP4's poster + encoded_video derivative
~10× faster than decoding the master. The asset row for the MP4 still
references the real master file — only ffmpeg's input changes.

Pairing is by **shared stem in the same directory** (case-insensitive on
the extension). DJI's filename already carries a unique timestamp + idx,
so two unrelated clips never collide. LRFs without a matching MP4
master (orphans from a deleted master or a copy glitch) return no pair
and should not be ingested as their own assets — they're proxies, not
content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


_MASTER_EXTS = {".mp4", ".mov"}
_PROXY_EXT = ".lrf"


def classify(path: Path) -> tuple[str, tuple[str, str]] | None:
    """Return ("master"|"proxy", key) or None.

    `key` is `(parent_dir_posix, stem_lower)` — the pairing identity.
    Classification is extension-only: `.lrf` is always a proxy; `.mp4`
    / `.mov` are potential masters (the `proxy_for` lookup filters
    masters that actually have a sibling LRF).
    """
    ext = path.suffix.lower()
    key = (path.parent.as_posix(), path.stem.lower())
    if ext == _PROXY_EXT:
        return "proxy", key
    if ext in _MASTER_EXTS:
        return "master", key
    return None


def build_proxy_index(
    paths: Iterable[Path],
) -> dict[tuple[str, str], Path]:
    """Map `(parent, stem_lower)` → LRF path, but only for pairs where
    a sibling `.mp4`/`.mov` master also exists.

    Orphan LRFs (no matching master in the same directory) are
    intentionally excluded so `is_paired_proxy` returns False for them
    — the caller keeps them in the row list so the user can see and
    investigate stray proxies.

    If two LRFs somehow share a key the first by sort order wins,
    matching the Insta360 helper's behavior.
    """
    master_keys: set[tuple[str, str]] = set()
    proxy_candidates: dict[tuple[str, str], Path] = {}
    for path in sorted(paths):
        hit = classify(path)
        if hit is None:
            continue
        role, key = hit
        if role == "master":
            master_keys.add(key)
        elif role == "proxy":
            proxy_candidates.setdefault(key, path)
    return {k: p for k, p in proxy_candidates.items() if k in master_keys}


def proxy_for(
    master_path: Path, index: dict[tuple[str, str], Path],
) -> Path | None:
    """Return the sibling LRF for a master video, or None.

    Non-masters (images, LRFs themselves) return None so callers don't
    accidentally redirect a proxy asset's derivatives to itself.
    """
    hit = classify(master_path)
    if hit is None or hit[0] != "master":
        return None
    return index.get(hit[1])


def is_paired_proxy(
    path: Path, index: dict[tuple[str, str], Path],
) -> bool:
    """True if `path` is an LRF proxy that has a sibling master in the
    index — i.e. it should be skipped at ingest time because the master
    carries the content and the LRF only exists to accelerate ffmpeg.

    An orphan LRF (no matching MP4 in the same directory) returns False
    — callers may still want to surface it so the user notices stray
    proxies, but it won't be in `index` either way.
    """
    hit = classify(path)
    if hit is None or hit[0] != "proxy":
        return False
    return hit[1] in index
