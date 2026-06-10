#!/usr/bin/env python3
"""Strip Whisper hallucination cues from `.srt` sidecars.

Whisper occasionally emits training-corpus boilerplate (fansub credits,
YouTube outros, applause/music tags) on silent or noisy audio — none of
which was actually said in the source video. This script walks every
trip under TRIPS_ROOT, parses each `<stem>.<lang>.srt` sidecar, and:

  1. Collapses word-level decode loops inside a cue («селфи» ×55 in one
     segment) to their first occurrence.
  2. Drops cues whose normalised text matches a known-hallucination
     pattern (regex or exact phrase, case-insensitive), and decode-loop
     cue repeats (same cue ≥6× consecutively) past the first.
  3. If anything remains, rewrites the sidecar with renumbered cues.
  4. If nothing remains, deletes the sidecar AND clears the matching
     `transcript` journal entry + invalidates the trip's `y_processed`
     marker so the next `immy process --with-transcripts` run
     re-transcribes the asset.

Dry-run by default — pass `--apply` to actually modify files.

Usage:
  tools/scrub-srt-hallucinations.py
  tools/scrub-srt-hallucinations.py --apply
  tools/scrub-srt-hallucinations.py --trip 2025-foo
  TRIPS_ROOT=/other/path tools/scrub-srt-hallucinations.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IMMY_SRC = SCRIPT_DIR.parent / "immy" / "src"
sys.path.insert(0, str(IMMY_SRC))

# Needs immy's deps (psycopg via immy.process) — re-exec under the immy
# venv when launched with a bare system python.
_VENV_PY = SCRIPT_DIR.parent / "immy" / ".venv" / "bin" / "python"
if _VENV_PY.is_file() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()),
                             *sys.argv[1:]])

from immy.hallucinations import (  # noqa: E402
    collapse_word_runs,
    is_hallucination,
    repetition_loop_indexes,
)
from immy.journal import Journal, journal_path  # noqa: E402
from immy.process import marker_path  # noqa: E402


def parse_srt(text: str) -> list[dict]:
    """Parse SRT into [{idx, time, lines:[...]}, ...] keeping cue text
    as raw lines so we can decide cue-by-cue whether to keep it."""
    cues: list[dict] = []
    block: list[str] = []
    for raw in text.splitlines():
        if raw.strip() == "":
            if block:
                cues.append(_block_to_cue(block))
                block = []
        else:
            block.append(raw)
    if block:
        cues.append(_block_to_cue(block))
    return [c for c in cues if c is not None]


def _block_to_cue(block: list[str]) -> dict | None:
    if len(block) < 2:
        return None
    # block[0] is the cue index, block[1] is the time line.
    if "-->" not in block[1]:
        # Some malformed sidecars omit the index; tolerate.
        if "-->" in block[0]:
            return {"time": block[0], "lines": block[1:]}
        return None
    return {"time": block[1], "lines": block[2:]}


def render_srt(cues: list[dict]) -> str:
    out: list[str] = []
    for i, cue in enumerate(cues, 1):
        out.append(str(i))
        out.append(cue["time"])
        out.extend(cue["lines"])
        out.append("")
    return "\n".join(out)


def collapse_repetition_loops(cues: list[dict]) -> tuple[list[dict], int]:
    """Drop decode-loop repeats (same cue stuck on repeat — see
    `hallucinations.repetition_loop_indexes`). Returns (kept, removed)."""
    texts = [" ".join(l for l in cue["lines"] if l.strip()) for cue in cues]
    drop = repetition_loop_indexes(texts)
    if not drop:
        return (cues, 0)
    kept = [cue for i, cue in enumerate(cues) if i not in drop]
    return (kept, len(drop))


def scrub_file(srt: Path, apply: bool) -> tuple[int, int, bool]:
    """Returns (cues_removed, cues_kept, file_deleted)."""
    text = srt.read_text(errors="replace")
    cues = parse_srt(text)
    if not cues:
        return (0, 0, False)
    kept: list[dict] = []
    removed = 0
    word_runs_collapsed = 0
    for cue in cues:
        # Word-level loops inside one cue («селфи» ×55 in a 30 s
        # segment) are invisible to the cue-level collapse — squash
        # them first so the cue keeps its real prefix.
        new_lines = [collapse_word_runs(l) for l in cue["lines"]]
        if new_lines != cue["lines"]:
            cue = {**cue, "lines": new_lines}
            word_runs_collapsed += 1
        # A cue with multiple text lines is hallucination only if EVERY
        # line is hallucinated — otherwise we'd drop legit lines that
        # happen to share a cue with junk.
        body_lines = [l for l in cue["lines"] if l.strip()]
        if body_lines and all(is_hallucination(l) for l in body_lines):
            removed += 1
            continue
        kept.append(cue)
    kept, loop_removed = collapse_repetition_loops(kept)
    removed += loop_removed
    if removed == 0 and word_runs_collapsed == 0:
        return (0, len(cues), False)
    removed += word_runs_collapsed  # collapsed-in-place cues count as scrubbed
    if not kept:
        if apply:
            srt.unlink()
        return (removed, 0, True)
    if apply:
        srt.write_text(render_srt(kept), encoding="utf-8")
    return (removed, len(kept), False)


def find_trips(trips_root: Path, only: str | None) -> list[Path]:
    if only:
        p = trips_root / only
        if not p.is_dir():
            sys.exit(f"no such trip: {p}")
        return [p]
    return sorted(
        d for d in trips_root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def clear_journal_for(trip: Path, srt: Path, apply: bool) -> bool:
    """When a sidecar is deleted, drop the matching transcript record so
    the asset is re-queued. Returns True if any entry was cleared."""
    jp = journal_path(trip)
    if not jp.is_file():
        return False
    j = Journal.load(trip)
    target = str(srt)
    cleared = False
    for cs, workers in list(j.entries.items()):
        rec = workers.get("transcript")
        if not rec:
            continue
        meta = rec.get("meta") or {}
        if str(meta.get("path") or "") == target:
            if apply:
                j.clear_worker(cs, "transcript")
            cleared = True
    if apply and cleared:
        j.flush()
    return cleared


def process_trip(trip: Path, apply: bool) -> tuple[int, int, int, int]:
    """Returns (files_touched, cues_removed, files_deleted, marker_invalidated)."""
    srts = sorted(p for p in trip.rglob("*.srt") if len(p.suffixes) >= 2)
    files_touched = cues_removed = files_deleted = 0
    for srt in srts:
        # Only sidecars with a 2–3 letter language tag — skip DJI telemetry.
        lang = srt.suffixes[-2].lstrip(".").lower()
        if not (2 <= len(lang) <= 3 and lang.isalpha()):
            continue
        removed, kept, deleted = scrub_file(srt, apply)
        if removed == 0:
            continue
        rel = os.path.relpath(srt, trip)
        files_touched += 1
        cues_removed += removed
        if deleted:
            files_deleted += 1
            print(f"  DEL  {rel}  (-{removed} cues, all hallucinated)")
            clear_journal_for(trip, srt, apply)
        else:
            print(f"  fix  {rel}  (-{removed} cues, {kept} kept)")
    marker_invalidated = 0
    if files_deleted and apply:
        mp = marker_path(trip)
        if mp.is_file():
            try:
                mp.unlink()
                marker_invalidated = 1
            except OSError as e:
                print(f"  !! could not delete {mp}: {e}")
    elif files_deleted:
        marker_invalidated = 1  # would invalidate
    return (files_touched, cues_removed, files_deleted, marker_invalidated)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trips-root", type=Path,
                    default=Path(os.environ.get("TRIPS_ROOT",
                                                Path.home() / "Media" / "Trips")))
    ap.add_argument("--trip", help="single trip folder name (under trips-root)")
    ap.add_argument("--apply", action="store_true",
                    help="actually rewrite/delete sidecars")
    args = ap.parse_args()

    print(f"Mode: {'APPLY (modifying)' if args.apply else 'DRY RUN'}")
    print(f"Trips root: {args.trips_root}")
    print()

    trips = find_trips(args.trips_root, args.trip)
    tot_files = tot_cues = tot_del = tot_marker = 0
    for trip in trips:
        ft, cr, fd, mi = process_trip(trip, args.apply)
        if ft:
            print(f"{trip.name}: {ft} files touched, {cr} cues, {fd} deleted")
            print()
        tot_files += ft
        tot_cues += cr
        tot_del += fd
        tot_marker += mi

    print("---")
    verb = "Removed" if args.apply else "Would remove"
    print(f"{verb} {tot_cues} hallucinated cues across {tot_files} sidecars; "
          f"{tot_del} sidecars went empty and were "
          f"{'deleted' if args.apply else 'flagged'}; "
          f"{tot_marker} trip marker(s) "
          f"{'invalidated' if args.apply else 'would be invalidated'}.")
    if not args.apply:
        print("Re-run with --apply to actually scrub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
