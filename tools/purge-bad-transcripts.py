#!/usr/bin/env python3
"""Find .srt sidecars whose detected language isn't one we actually
speak (en/ru/uk) and queue them for re-transcription.

Whisper's pre-fix language head was happy to label wind-noise clips as
`fo` / `nn` / `ja`. After the language-prior fix in transcripts.py, new
runs are constrained — but past runs left junk sidecars on disk plus
journal entries that say "transcript done at this version", so the
batch script will skip them on the next pass.

This script:
  1. Walks every trip under TRIPS_ROOT.
  2. Loads each `.audit/journal.yml` and finds `transcript` entries
     whose `meta.language` is outside the candidate set.
  3. Deletes the matching `<stem>.<lang>.srt` sidecar and clears the
     journal entry (so the next `immy process --with-transcripts` run
     re-transcribes the asset with the constrained detector).
  4. Also sweeps disk for orphan sidecars (no journal entry) in
     bad-language codes and removes them.

Dry-run by default — pass `--apply` to actually delete.

Usage:
  tools/purge-bad-transcripts.py                    # dry run
  tools/purge-bad-transcripts.py --apply            # delete + clear
  tools/purge-bad-transcripts.py --trip 2025-foo    # one trip only
  TRIPS_ROOT=/other/path tools/purge-bad-transcripts.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Reuse immy's Journal so we read/write the exact same shape the
# pipeline expects. Avoids hand-rolling YAML round-trips.
SCRIPT_DIR = Path(__file__).resolve().parent
IMMY_SRC = SCRIPT_DIR.parent / "immy" / "src"
sys.path.insert(0, str(IMMY_SRC))

from immy.journal import Journal, journal_path  # noqa: E402
from immy.process import marker_path  # noqa: E402
from immy.transcripts import DEFAULT_LANG_CANDIDATES  # noqa: E402


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


def purge_journal(trip: Path, allowed: set[str], apply: bool) -> tuple[int, int]:
    """Returns (bad_entries_found, sidecars_deleted_via_journal)."""
    jp = journal_path(trip)
    if not jp.is_file():
        return (0, 0)
    j = Journal.load(trip)
    bad: list[tuple[str, str, str]] = []  # (checksum, lang, sidecar_path)
    for cs, workers in j.entries.items():
        rec = workers.get("transcript")
        if not rec:
            continue
        meta = rec.get("meta") or {}
        lang = str(meta.get("language") or "").lower()
        if lang and lang not in allowed:
            bad.append((cs, lang, str(meta.get("path") or "")))
    if not bad:
        return (0, 0)
    deleted = 0
    for cs, lang, sidecar in bad:
        rel = os.path.relpath(sidecar, trip) if sidecar else "?"
        print(f"  bad srt:{lang:<3}  {rel}")
        if apply:
            if sidecar:
                try:
                    Path(sidecar).unlink()
                    deleted += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    print(f"    !! could not delete {sidecar}: {e}")
            j.clear_worker(cs, "transcript")
    if apply:
        j.flush()
    return (len(bad), deleted)


def purge_orphan_sidecars(trip: Path, allowed: set[str], apply: bool) -> int:
    """Find `*.xx.srt` files on disk where xx isn't allowed and isn't a
    DJI telemetry-style `.SRT` (which has no language infix). These can
    appear when transcripts.py was called outside the journal flow.
    """
    deleted = 0
    for srt in trip.rglob("*.srt"):
        # DJI telemetry siblings: bare `<stem>.SRT` / `<stem>.srt` —
        # only one suffix. Our sidecars have two (`<stem>.<lang>.srt`).
        if len(srt.suffixes) < 2:
            continue
        lang = srt.suffixes[-2].lstrip(".").lower()
        # Heuristic: language tags are 2–3 chars, lowercase letters only.
        # Anything else (e.g. `.thumb.srt` if such a thing existed) is
        # left alone.
        if not (2 <= len(lang) <= 3 and lang.isalpha()):
            continue
        if lang in allowed:
            continue
        rel = os.path.relpath(srt, trip)
        print(f"  orphan srt:{lang:<3} {rel}")
        if apply:
            try:
                srt.unlink()
                deleted += 1
            except OSError as e:
                print(f"    !! could not delete {srt}: {e}")
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trips-root", type=Path,
                    default=Path(os.environ.get("TRIPS_ROOT",
                                                Path.home() / "Media" / "Trips")))
    ap.add_argument("--trip", help="single trip folder name (under trips-root)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete sidecars + clear journal entries")
    ap.add_argument("--lang", action="append",
                    help="override allowed language codes (default: en ru uk); "
                         "repeat to add multiple")
    args = ap.parse_args()

    allowed = {l.lower() for l in (args.lang or DEFAULT_LANG_CANDIDATES)}
    print(f"Allowed languages: {sorted(allowed)}")
    print(f"Mode: {'APPLY (deleting)' if args.apply else 'DRY RUN'}")
    print(f"Trips root: {args.trips_root}")
    print()

    trips = find_trips(args.trips_root, args.trip)
    total_bad = total_deleted = total_orphans = total_markers = 0
    for trip in trips:
        bad, deleted = purge_journal(trip, allowed, args.apply)
        orphans = purge_orphan_sidecars(trip, allowed, args.apply)
        if bad or orphans:
            # The trip-level `y_processed.yml` marker is checked by
            # `is_trip_fully_cached` *before* per-asset journal logic
            # runs (cli.py: process_one). Without invalidating it, the
            # next batch run skips the whole trip and never re-reaches
            # the transcript phase we just cleared.
            mp = marker_path(trip)
            marker_removed = False
            if mp.is_file():
                if args.apply:
                    try:
                        mp.unlink()
                        marker_removed = True
                    except OSError as e:
                        print(f"  !! could not delete {mp}: {e}")
                else:
                    marker_removed = True
            extra = " + cleared y_processed.yml" if marker_removed else ""
            print(f"{trip.name}: {bad} journal entries, {orphans} orphan sidecars{extra}")
            print()
            if marker_removed:
                total_markers += 1
        total_bad += bad
        total_deleted += deleted
        total_orphans += orphans

    print("---")
    if args.apply:
        print(f"Deleted {total_deleted} journal-tracked sidecars + "
              f"{total_orphans} orphans, cleared {total_bad} journal entries, "
              f"invalidated {total_markers} trip marker(s).")
        print("Re-run tools/process-all-trips.sh to re-transcribe with the "
              "language-prior fix.")
    else:
        print(f"Would delete {total_bad} journal-tracked sidecars + "
              f"{total_orphans} orphans, clear {total_bad} journal entries, "
              f"invalidate {total_markers} trip marker(s).")
        print("Re-run with --apply to actually purge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
