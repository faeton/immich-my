#!/usr/bin/env python3
"""Remove duplicate per-lens Insta360 .srt sidecars.

Insta360 360 clips ship as a 3-file bundle that share the same audio
track — `VID_*_00_NNN.insv` (front), `VID_*_10_NNN.insv` (rear), and
`LRV_*_11_NNN.insv` (low-res proxy). An earlier Whisper pass (before
the Insta360 denylist) transcribed each independently and produced
three byte-identical `.<lang>.srt` sidecars per clip. Current immy is
gated by an ffprobe `has_audio` check, so re-runs won't regenerate
them, but the duplicates linger.

This script keeps the canonical sidecar (preferring `_00_` master,
then `_10_`, then the LRV proxy) and deletes the rest. Skips any
group whose .srt contents diverge so the user can resolve manually.

Usage:
    dedup-insta360-srt.py [<root>] [--apply]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path


CLIP_RE = re.compile(
    r"^(?P<role>VID|LRV)_(?P<dt>\d{8}_\d{6})_(?P<lens>\d{2})_(?P<seq>\d{3})\.(?P<lang>[a-z]{2,3})\.srt$",
    re.I,
)
LENS_PRIORITY = {"00": 0, "10": 1, "11": 2}  # lower wins


def md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="/Users/faeton/Media/Trips", type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    root: Path = args.root.resolve()
    # group_key: (parent_dir, dt, seq, lang) → list[Path]
    groups: dict[tuple[Path, str, str, str], list[Path]] = defaultdict(list)
    for p in root.rglob("*.srt"):
        if "/.audit/" in str(p):
            continue
        m = CLIP_RE.match(p.name)
        if not m:
            continue
        key = (p.parent, m.group("dt"), m.group("seq"), m.group("lang").lower())
        groups[key].append(p)

    candidates = {k: v for k, v in groups.items() if len(v) > 1}
    if not candidates:
        print("nothing to dedup")
        return 0

    total_groups = len(candidates)
    deletes: list[Path] = []
    skipped_diverge = 0
    for (parent, dt, seq, lang), files in sorted(candidates.items()):
        # Sort by lens-priority so the canonical winner is files[0]
        files.sort(key=lambda f: (
            LENS_PRIORITY.get(CLIP_RE.match(f.name).group("lens"), 99),
            f.name,
        ))
        hashes = {f: md5(f) for f in files}
        if len(set(hashes.values())) > 1:
            print(f"DIVERGE {parent.name}/{dt}_{seq}.{lang}.srt:")
            for f in files:
                print(f"   {hashes[f][:10]}  {f.name}")
            skipped_diverge += 1
            continue
        keep = files[0]
        for f in files[1:]:
            deletes.append(f)
        print(f"keep {parent.name}/{keep.name}  drop {len(files)-1}")

    print()
    print(f"groups: {total_groups}  diverged: {skipped_diverge}  to delete: {len(deletes)}")
    if not args.apply:
        print("(dry-run — pass --apply to delete)")
        return 0

    for f in deletes:
        f.unlink()
    print(f"deleted {len(deletes)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
