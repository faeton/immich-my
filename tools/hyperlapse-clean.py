#!/usr/bin/env python3
"""
Scan & delete DJI hyperlapse JPG piles.

DJI drones write every hyperlapse frame as a full-resolution JPG
(`HYPERLAPSE_0001.JPG`, …) next to the rendered MP4. Hundreds of
near-identical 8 MB stills per run → 1–2 GB each. Once the rendered
video exists, the JPG sequence is redundant. This tool finds them,
groups by folder, confirms per folder, deletes.

Usage:
    tools/hyperlapse-clean.py <folder>                  # interactive delete
    tools/hyperlapse-clean.py <folder> --list           # scan only
    tools/hyperlapse-clean.py <folder> --dry-run        # prompt but don't delete
    tools/hyperlapse-clean.py <folder> --yes            # skip prompts

Rendered-MP4 matching (requires exiftool + ffprobe on PATH):
  For each group we read the first JPG's `DateTimeOriginal` via
  exiftool, then ffprobe every candidate MP4/MOV within two directory
  levels above. A candidate matches when:
    - its `creation_time` is within 10 minutes of the first JPG, AND
    - its video `nb_frames` is within ±50 % of the JPG count.
  Exact frame match would be nicer, but JPGs sometimes go missing
  (failed transfer, manual culling), so a loose count window plus a
  tight time window is the robust signal. When multiple candidates
  pass we pick the one closest in creation_time.

  If exiftool/ffprobe aren't on PATH, matching is skipped and every
  prompt defaults to N — you still get the list + sizes, but without
  an automated "safe to delete" signal.

Safety:
  - Only files matching `HYPERLAPSE_####.JPG` (case-insensitive) are
    touched. Nothing else.
  - Prompt default is Y only when a confirmed rendered MP4 was found.
    Otherwise default is N and the prompt says so.
  - Per-run subfolders (`001_0006/`) are `rmdir`'d after emptying.
    `HYPERLAPSE/` and higher dirs are left alone.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


_FRAME_RE = re.compile(r"^HYPERLAPSE_\d+\.JPE?G$", re.IGNORECASE)
_MP4_EXTS = {".mp4", ".mov"}
_MP4_SEARCH_DEPTH = 2                    # folder + two ancestors
_CREATION_TIME_TOLERANCE = timedelta(minutes=10)
_FRAME_COUNT_TOLERANCE = 0.50            # |nb_frames - jpg_count| / jpg_count
# Prune candidates by filesystem mtime before ffprobe — generous window
# (copies can skew mtime a bit) but small enough to cut hundreds of
# unrelated drone clips down to a handful.
_MTIME_PREFILTER_WINDOW = timedelta(minutes=30)


def is_frame(path: Path) -> bool:
    return _FRAME_RE.match(path.name) is not None


@dataclass
class Group:
    folder: Path
    frames: list[Path]
    candidate_mp4s: list[Path] = field(default_factory=list)
    matched_mp4: Path | None = None           # confirmed rendered video
    first_jpg_time: datetime | None = None    # naive local from EXIF

    @property
    def total_bytes(self) -> int:
        total = 0
        for p in self.frames:
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    @property
    def count(self) -> int:
        return len(self.frames)


# --- discovery ------------------------------------------------------------


def find_candidate_mp4s(folder: Path, depth: int = _MP4_SEARCH_DEPTH) -> list[Path]:
    """Scan folder + `depth` ancestors, and peek one level into each
    ancestor's sibling subdirs, for MP4/MOV candidates.

    The sibling-subdir peek is what catches layouts where the rendered
    videos live in a neighbour folder (e.g. `<trip>/DJI_001/*.MP4`
    next to `<trip>/HYPERLAPSE/001_xxxx/*.JPG`). We go only one level
    deep per sibling so we don't walk an entire archive tree.

    The mtime prefilter in the match step keeps this from becoming a
    performance issue even when an ancestor holds hundreds of clips.
    """
    seen: set[Path] = set()
    visited_dirs: set[Path] = set()

    def add_mp4s_in(d: Path) -> None:
        if d in visited_dirs or not d.is_dir():
            return
        visited_dirs.add(d)
        try:
            for child in d.iterdir():
                if child.is_file() and child.suffix.lower() in _MP4_EXTS:
                    seen.add(child)
        except OSError:
            pass

    cur = folder
    for level in range(depth + 1):
        add_mp4s_in(cur)
        # At ancestor levels (not the frames folder itself), also peek
        # one level into each sibling subdir — that's where drones
        # often drop the rendered MP4 in a `DJI_001` / `100MEDIA`
        # neighbour of `HYPERLAPSE/`.
        if level > 0:
            try:
                for sibling in cur.iterdir():
                    if sibling.is_dir():
                        add_mp4s_in(sibling)
            except OSError:
                pass
        if cur.parent == cur:
            break
        cur = cur.parent
    return sorted(seen)


def scan(root: Path) -> list[Group]:
    buckets: dict[Path, list[Path]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or not is_frame(path):
            continue
        buckets.setdefault(path.parent, []).append(path)
    return [
        Group(
            folder=folder,
            frames=sorted(buckets[folder]),
            candidate_mp4s=find_candidate_mp4s(folder),
        )
        for folder in sorted(buckets)
    ]


# --- exiftool + ffprobe wrappers -----------------------------------------


def _parse_exif_datetime(s: str) -> datetime | None:
    """DJI writes `2025:06:14 12:25:15` (naive local). Return naive datetime."""
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def read_first_jpg_time(jpg: Path) -> datetime | None:
    cmd = ["exiftool", "-j", "-DateTimeOriginal", str(jpg)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    return _parse_exif_datetime(data[0].get("DateTimeOriginal", ""))


def _parse_ffprobe_creation_time(s: str) -> datetime | None:
    """ffprobe reports UTC ISO like `2025-06-14T10:25:13.000000Z`. Return
    a naive *local* datetime so we can compare to exif's naive local."""
    if not s:
        return None
    try:
        # Normalise Z → +00:00 for fromisoformat; strip sub-second if odd.
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    local = dt.astimezone()                          # convert to system tz
    return local.replace(tzinfo=None)


@dataclass
class Probe:
    nb_frames: int | None
    creation_time: datetime | None                   # naive local


def probe_mp4(path: Path) -> Probe | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:format_tags=creation_time:stream=codec_type,nb_frames",
        "-of", "json", str(path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    nb_frames: int | None = None
    for s in data.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        raw = s.get("nb_frames")
        if raw is None:
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        # Drones emit a second "video" stream (thumbnail) with bogus
        # frame counts; take the first plausible one.
        if n > 1:
            nb_frames = n
            break
    ct = _parse_ffprobe_creation_time(
        (data.get("format", {}).get("tags") or {}).get("creation_time", "")
    )
    return Probe(nb_frames=nb_frames, creation_time=ct)


# --- matching -------------------------------------------------------------


def match_rendered_mp4(group: Group) -> None:
    """Populate group.matched_mp4 + group.first_jpg_time by probing candidates.

    Loose frame count (±50 %) + tight creation_time window (±10 min).
    Frames sometimes go missing from the JPG folder (failed transfer,
    manual cull), so exact count is too strict. Of the candidates that
    pass both gates we pick the one whose creation_time is closest to
    the first JPG — the drone stamps the rendered file at the start of
    the run, so the closest stamp is almost always the right clip.
    """
    if not group.frames:
        return
    group.first_jpg_time = read_first_jpg_time(group.frames[0])
    if group.first_jpg_time is None:
        return

    best: tuple[timedelta, Path] | None = None
    for mp4 in group.candidate_mp4s:
        # Cheap prefilter: file mtime far from JPG time → can't be the
        # rendered output. Saves an ffprobe per unrelated clip in
        # ancestor folders with lots of drone files.
        try:
            mtime = datetime.fromtimestamp(mp4.stat().st_mtime)
        except OSError:
            continue
        if abs(mtime - group.first_jpg_time) > _MTIME_PREFILTER_WINDOW:
            continue
        probe = probe_mp4(mp4)
        if probe is None or probe.creation_time is None:
            continue
        delta = abs(probe.creation_time - group.first_jpg_time)
        if delta > _CREATION_TIME_TOLERANCE:
            continue
        if probe.nb_frames is not None and group.count > 0:
            ratio = abs(probe.nb_frames - group.count) / group.count
            if ratio > _FRAME_COUNT_TOLERANCE:
                continue
        if best is None or delta < best[0]:
            best = (delta, mp4)
    if best is not None:
        group.matched_mp4 = best[1]


# --- deletion -------------------------------------------------------------


def delete_group(group: Group, *, remove_empty_dir: bool = True) -> int:
    reclaimed = 0
    for p in group.frames:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        try:
            p.unlink()
            reclaimed += sz
        except OSError:
            continue
    if remove_empty_dir:
        try:
            next(group.folder.iterdir())
        except StopIteration:
            try:
                group.folder.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    return reclaimed


# --- output ---------------------------------------------------------------


def fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


def rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return str(p)


def print_summary(root: Path, groups: list[Group], *, matching_on: bool) -> None:
    if not groups:
        print("no hyperlapse JPGs found.")
        return
    total_bytes = sum(g.total_bytes for g in groups)
    total_frames = sum(g.count for g in groups)
    print(
        f"\n{total_frames} frame(s) across {len(groups)} folder(s)  "
        f"total: {fmt_bytes(total_bytes)}"
    )
    for g in groups:
        if not matching_on:
            tag = "matching skipped"
        elif g.matched_mp4 is not None:
            tag = f"MATCH {rel(g.matched_mp4, root)}"
        else:
            tag = "no matching rendered MP4"
        print(
            f"\n  {rel(g.folder, root)} — {g.count} frame(s), "
            f"{fmt_bytes(g.total_bytes)}  [{tag}]"
        )


def prompt_yn(msg: str, default: str) -> bool:
    suffix = "[Y/n]" if default == "y" else "[y/N]"
    try:
        answer = input(f"{msg} {suffix} ").strip().lower()
    except EOFError:
        return False
    if not answer:
        answer = default
    return answer in ("y", "yes")


# --- driver ---------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Scan & delete DJI hyperlapse JPG piles."
    )
    parser.add_argument("folder", type=Path, help="Folder to scan (recursive).")
    parser.add_argument(
        "--list", action="store_true", dest="list_only",
        help="Scan, match, print summary; don't prompt or delete.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Prompt per folder, but don't actually delete.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip per-group confirmation. Dangerous without --dry-run.",
    )
    parser.add_argument(
        "--no-match", action="store_true",
        help="Skip exiftool/ffprobe match step (faster, prompts default N).",
    )
    args = parser.parse_args(argv)

    root: Path = args.folder.resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    matching_on = not args.no_match
    if matching_on:
        missing = [b for b in ("exiftool", "ffprobe") if shutil.which(b) is None]
        if missing:
            print(
                f"warn: {', '.join(missing)} not on PATH — skipping match step.",
                file=sys.stderr,
            )
            matching_on = False

    groups = scan(root)
    if matching_on:
        for i, g in enumerate(groups, 1):
            print(f"  matching {i}/{len(groups)}: {rel(g.folder, root)}", file=sys.stderr)
            match_rendered_mp4(g)

    print_summary(root, groups, matching_on=matching_on)
    if not groups or args.list_only:
        return 0

    accepted: list[Group] = []
    for g in groups:
        if args.yes:
            accepted.append(g)
            continue
        matched = g.matched_mp4 is not None
        default = "y" if matched else "n"
        warn = (
            f" [match: {rel(g.matched_mp4, root)}]"
            if matched
            else " [NO match — keep?]"
        )
        if prompt_yn(
            f"\ndelete {rel(g.folder, root)}{warn} "
            f"({g.count} frame(s), {fmt_bytes(g.total_bytes)})?",
            default=default,
        ):
            accepted.append(g)

    if not accepted:
        print("nothing accepted.")
        return 0

    if args.dry_run:
        total = sum(g.total_bytes for g in accepted)
        frames = sum(g.count for g in accepted)
        print(f"\ndry-run: would delete {frames} frame(s), {fmt_bytes(total)}")
        return 0

    reclaimed_total = 0
    for g in accepted:
        reclaimed = delete_group(g)
        reclaimed_total += reclaimed
        print(f"  ✓ {rel(g.folder, root)}  ({fmt_bytes(reclaimed)})")
    print(f"\nreclaimed {fmt_bytes(reclaimed_total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
