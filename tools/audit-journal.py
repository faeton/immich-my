#!/usr/bin/env python3
"""True per-trip enrichment coverage, keyed the way the pipeline keys it.

`journal.yml` is keyed by sha1("path:" + container_path), so renaming or
merging a trip orphans every old entry — naive journal scans then report
thousands of "missing" captions/faces that are really just stale keys
(and conversely, captions recorded only in the offline sink make the
journal under-report). This tool walks the *files on disk*, applies the
same ingest filters as `immy process` (DJI .LRF proxies dropped,
RAW-paired JPEG previews dropped), maps each file to its journal entry,
and reports what is genuinely missing per phase:

  derivatives  — expected for every asset
  clip / faces — expected for IMAGE assets only (videos are server-side)
  caption      — expected for images, and for videos with a poster
                 preview; assets whose description is user-typed or a
                 Whisper transcript are *blocked by design* and counted
                 separately, not as missing

Stale journal entries (keys matching no live file) are counted per trip;
`--prune-stale` lists them and `--prune-stale --apply` removes them from
journal.yml (offline sink YAMLs are never touched — they may still be
the only record linking a synced DB asset).

Usage:
  tools/audit-journal.py                      # coverage report, all trips
  tools/audit-journal.py --trip 2025-foo      # one trip
  tools/audit-journal.py -v                   # list every missing file
  tools/audit-journal.py --prune-stale        # show stale keys (dry run)
  tools/audit-journal.py --prune-stale --apply
  TRIPS_ROOT=/other/path tools/audit-journal.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IMMY_SRC = SCRIPT_DIR.parent / "immy" / "src"
sys.path.insert(0, str(IMMY_SRC))

# Needs immy's deps (numpy, yaml) — re-exec under the immy venv when
# launched with a bare system python.
_VENV_PY = SCRIPT_DIR.parent / "immy" / ".venv" / "bin" / "python"
if _VENV_PY.is_file() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()),
                             *sys.argv[1:]])

import yaml  # noqa: E402

from immy import dji as dji_mod  # noqa: E402
from immy import offline as offline_mod  # noqa: E402
from immy import raw as raw_mod  # noqa: E402
from immy.captions import is_ai_description  # noqa: E402
from immy.exif import iter_media  # noqa: E402
from immy.journal import Journal, journal_path  # noqa: E402
from immy.process import asset_type_for, container_path_for, path_checksum  # noqa: E402

TRIPS_ROOT = Path(os.environ.get("TRIPS_ROOT", str(Path.home() / "Media" / "Trips")))

PHASES = ("derivatives", "clip", "faces", "caption")


def find_trips(only: str | None) -> list[Path]:
    if only:
        p = TRIPS_ROOT / only
        if not p.is_dir():
            sys.exit(f"no such trip: {p}")
        return [p]
    return sorted(
        d for d in TRIPS_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".") and journal_path(d).is_file()
    )


def container_root_for(trip: Path) -> tuple[str, str] | None:
    """Returns (container_root, source). The marker is authoritative —
    it records the exact root the journal keys were hashed with; the
    cached library is a fallback that can mismatch after a root
    migration, so the source is surfaced in output and gates pruning."""
    root = offline_mod.derive_container_root_from_marker(trip)
    if root:
        return root, "marker"
    lib = offline_mod.load_cached_library()
    if lib is not None:
        return lib.container_root, "cached-library"
    return None


def sink_entry(trip: Path, cs_hex: str) -> dict:
    p = trip / ".audit" / "offline" / f"{cs_hex}.yml"
    if not p.is_file():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def audit_trip(trip: Path, verbose: bool) -> dict | None:
    derived = container_root_for(trip)
    if derived is None:
        print(f"  {trip.name}: no marker/cached library — cannot derive container root, skipped")
        return None
    root, root_source = derived
    journal = Journal.load(trip)
    files = list(iter_media(trip))
    raw_index = raw_mod.build_raw_index(files)
    assets = [
        f for f in files
        if not dji_mod.is_proxy(f) and not raw_mod.is_paired_preview(f, raw_index)
    ]
    live: set[str] = set()
    missing: dict[str, list[str]] = {ph: [] for ph in PHASES}
    blocked: list[str] = []  # caption blocked by user/Whisper description
    sink_only: list[str] = []  # captioned in sink, journal lagging
    for f in assets:
        cs = path_checksum(container_path_for(f, trip, root)).hex()
        live.add(cs)
        rel = f.relative_to(trip).as_posix()
        workers = journal.entries.get(cs, {})
        if "derivatives" not in workers:
            missing["derivatives"].append(rel)
            continue
        kinds = {
            d.get("kind")
            for d in (workers["derivatives"].get("meta") or {}).get("files") or []
        }
        atype = asset_type_for(f.suffix)
        if atype == "IMAGE":
            for ph in ("clip", "faces"):
                if ph not in workers:
                    missing[ph].append(rel)
        if "caption" in workers:
            continue
        if not (atype == "IMAGE" or "preview" in kinds):
            continue  # video with no poster still — not captionable
        sink = sink_entry(trip, cs)
        desc = (sink.get("exif") or {}).get("description") or ""
        if sink.get("caption") or is_ai_description(desc):
            sink_only.append(rel)
        elif desc.strip():
            blocked.append(rel)
        else:
            missing["caption"].append(rel)
    stale = sorted(set(journal.entries) - live)
    counts = {ph: len(missing[ph]) for ph in PHASES}
    if verbose:
        for ph in PHASES:
            for rel in missing[ph]:
                print(f"    missing {ph}: {trip.name}/{rel}")
        for rel in blocked:
            print(f"    caption blocked by description: {trip.name}/{rel}")
        for rel in sink_only:
            print(f"    captioned in sink only: {trip.name}/{rel}")
    return {
        "trip": trip.name,
        "assets": len(assets),
        "missing": counts,
        "blocked": len(blocked),
        "sink_only": len(sink_only),
        "stale": stale,
        "matched": len(live & set(journal.entries)),
        "root": root,
        "root_source": root_source,
    }


def prune_stale(trip: Path, r: dict, apply: bool) -> None:
    stale = r["stale"]
    if not stale:
        return
    print(f"  {r['trip']}: {len(stale)} stale journal entr{'y' if len(stale) == 1 else 'ies'}"
          f"  (root={r['root']} via {r['root_source']}, {r['matched']} live keys matched)")
    # Wrong container root makes EVERY live file hash differently — all
    # journal keys then look stale and --apply would wipe the journal.
    # Zero live matches alongside a non-empty journal is exactly that
    # signature; legitimate rename leftovers always coexist with matched
    # live keys.
    if apply and r["assets"] > 0 and r["matched"] == 0:
        print("    !! REFUSING to prune: no live file matches any journal key — the derived "
              "container root is almost certainly wrong for this trip.")
        return
    journal = Journal.load(trip)
    for cs in stale:
        workers = sorted(journal.entries.get(cs, {}))
        print(f"    {cs[:12]}…  workers={workers}")
        if apply:
            for w in list(journal.entries.get(cs, {})):
                journal.clear_worker(cs, w)
    if apply:
        journal.flush()
        print(f"    pruned {len(stale)} → {journal_path(trip)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trip", help="audit a single trip folder name")
    ap.add_argument("-v", "--verbose", action="store_true", help="list each missing/blocked file")
    ap.add_argument("--prune-stale", action="store_true", help="list stale journal entries (dry run)")
    ap.add_argument("--apply", action="store_true", help="with --prune-stale: actually delete them")
    args = ap.parse_args()
    if args.apply and not args.prune_stale:
        sys.exit("--apply only makes sense with --prune-stale")
    if not TRIPS_ROOT.is_dir():
        sys.exit(f"trips root not found: {TRIPS_ROOT}")

    rows = []
    for trip in find_trips(args.trip):
        row = audit_trip(trip, args.verbose)
        if row is not None:
            rows.append(row)

    totals = {ph: 0 for ph in PHASES}
    blocked_total = sink_only_total = stale_total = assets_total = 0
    print(f"\n{'trip':36s} {'assets':>6s} {'deriv':>6s} {'clip':>5s} {'faces':>5s} {'capt':>5s} {'blckd':>5s} {'stale':>5s}")
    for r in rows:
        m = r["missing"]
        flagged = any(m.values()) or r["stale"]
        if flagged or args.trip:
            print(f"{r['trip']:36s} {r['assets']:6d} {m['derivatives']:6d} {m['clip']:5d} "
                  f"{m['faces']:5d} {m['caption']:5d} {r['blocked']:5d} {len(r['stale']):5d}")
        for ph in PHASES:
            totals[ph] += m[ph]
        blocked_total += r["blocked"]
        sink_only_total += r["sink_only"]
        stale_total += len(r["stale"])
        assets_total += r["assets"]
    print(f"{'TOTAL (' + str(len(rows)) + ' trips)':36s} {assets_total:6d} {totals['derivatives']:6d} "
          f"{totals['clip']:5d} {totals['faces']:5d} {totals['caption']:5d} {blocked_total:5d} {stale_total:5d}")
    if sink_only_total:
        print(f"\n{sink_only_total} asset(s) captioned in the offline sink but not yet journaled "
              f"(a `--reprocess` pass converges these).")
    print("\ncolumns: deriv/clip/faces/capt = genuinely missing; blckd = caption blocked by "
          "user/Whisper description (by design); stale = journal keys with no live file.")

    if totals["derivatives"] == totals["clip"] == totals["faces"] == totals["caption"] == 0:
        print("verdict: all live assets fully enriched — nothing left to process.")

    if args.prune_stale:
        print()
        any_stale = False
        for r in rows:
            if r["stale"]:
                any_stale = True
                prune_stale(TRIPS_ROOT / r["trip"], r, args.apply)
        if not any_stale:
            print("no stale journal entries found.")
        elif not args.apply:
            print("\ndry run — re-run with --apply to prune.")


if __name__ == "__main__":
    main()
