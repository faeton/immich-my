#!/usr/bin/env python3
"""Pipelined overnight ingest: ONE sequential `immy process` overlapped with a
parallel `immy promote` pool, so the laptop's cores keep transcoding trip B
while the uplink uploads trip A.

WHY THIS SHAPE (settled with a Codex + Grok review):
    - PROCESS is NOT parallelized. The ML models (CLIP via MLX, faces via
      Vision/InsightFace, and the optional LM Studio captioner) load once per
      invocation and run on the single Apple Neural Engine — parallel process
      workers would duplicate the models in RAM and thrash the one accelerator.
      ffmpeg's 720p transcode already parallelizes internally across all cores.
      So process is a SINGLE `immy process trip1 trip2 …` invocation (models
      loaded once, trips sequential, ffmpeg using the cores).
    - The parallelism that matters is BETWEEN stages: while that one process
      stream works on the next trip, a pool of `immy promote` workers uploads
      the trips that are already done. Multiple rsync streams beat single-
      stream TCP collapse on the variable uplink (same reason as
      promote-parallel.py).
    - `--offline`: process caches asset data to .audit/offline/ and stages
      derivatives WITHOUT inserting Immich rows. promote rsyncs the originals
      first, THEN drains the cache — so DB rows never appear before the files
      land (avoids the offline/placeholder-thumbnail mess).
    - Resumable: a trip is handed to the promote pool only once its
      `.audit/y_processed.yml` marker exists; already-promoted trips are
      skipped. Safe to Ctrl-C and re-run — process skips done trips via its
      journal, promote resumes via rsync --partial.

USAGE
    tools/overnight.py                     # all pending trips: process(offline) + parallel upload
    tools/overnight.py --captions          # also run the LM Studio VLM captioner (SLOW, ~60s/img)
    tools/overnight.py --no-transcode      # skip the 720p web transcode
    tools/overnight.py -P 6                # 6 concurrent upload streams (default 4)
    tools/overnight.py '2025-11-pacific-*' # subset by glob
    tools/overnight.py --status            # show what's pending and exit

ENV: TRIPS_ROOT (default ~/Media/Trips), IMMY_CONFIG (default ~/.immy/config.yml).
"""

from __future__ import annotations

import argparse
import glob
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRIPS_ROOT = Path(os.environ.get("TRIPS_ROOT", str(Path.home() / "Media" / "Trips")))
LOG_ROOT = Path.home() / ".immy" / "promote-logs"
IMMY = os.path.join(_REPO, "immy", ".venv", "bin", "immy")
if not os.path.exists(IMMY):
    import shutil
    IMMY = shutil.which("immy") or IMMY

# Multi-stream ssh tuning (ControlMaster=no is load-bearing: with multiplexing
# every parallel rsync rides ONE TCP and the fan-out is wasted).
SSH_OPTS = ("-o ControlMaster=no -o ControlPath=none -o Compression=no "
            "-o IPQoS=throughput -o ServerAliveInterval=15 -o ServerAliveCountMax=3")


def _is_promoted(trip: Path) -> bool:
    audit = trip / ".audit" / "audit.jsonl"
    try:
        return audit.is_file() and '"promoted"' in audit.read_text()
    except OSError:
        return False


def _is_processed(trip: Path) -> bool:
    return (trip / ".audit" / "y_processed.yml").is_file()


def _size_gb(trip: Path) -> float:
    try:
        out = subprocess.run(["du", "-sk", str(trip)], capture_output=True, text=True)
        return int(out.stdout.split()[0]) / (1024 * 1024)
    except (ValueError, IndexError, subprocess.SubprocessError):
        return 0.0


def discover(patterns: list[str], *, force: bool) -> list[Path]:
    if patterns:
        dirs: list[Path] = []
        seen = set()
        for pat in patterns:
            for d in sorted(glob.glob(str(TRIPS_ROOT / pat))):
                p = Path(d)
                if p.is_dir() and p not in seen:
                    seen.add(p)
                    dirs.append(p)
        if not dirs:
            print(f"no trips matched: {patterns}", file=sys.stderr)
            sys.exit(1)
    else:
        dirs = sorted(p for p in TRIPS_ROOT.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    pending = [d for d in dirs if force or not _is_promoted(d)]
    # LPT: biggest first so a 300 GB trip's long process+upload starts early.
    pending.sort(key=lambda d: -_size_gb(d))
    return pending


def main() -> None:
    ap = argparse.ArgumentParser(description="Pipelined overnight process + parallel upload.")
    ap.add_argument("patterns", nargs="*", help="trip name(s)/glob(s); default: all pending")
    ap.add_argument("-P", "--promote-workers", type=int, default=4, help="concurrent upload streams (default 4)")
    ap.add_argument("--captions", action="store_true", help="also run the LM Studio VLM captioner (slow)")
    ap.add_argument("--no-transcode", action="store_true", help="skip the 720p web transcode")
    ap.add_argument("--force", action="store_true", help="redo trips already promoted")
    ap.add_argument("--status", action="store_true", help="print pending trips and exit")
    args = ap.parse_args()

    if not TRIPS_ROOT.is_dir():
        print(f"trips root not found: {TRIPS_ROOT}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(IMMY):
        print(f"immy not found: {IMMY}", file=sys.stderr)
        sys.exit(1)

    trips = discover(args.patterns, force=args.force)
    if args.status:
        total = sum(_size_gb(t) for t in trips)
        print(f"{len(trips)} pending trip(s), {total:.0f} GB ({total/1024:.2f} TB):")
        for t in trips:
            tag = "processed" if _is_processed(t) else "needs process"
            print(f"  {_size_gb(t):7.1f}G  {t.name:32s} [{tag}]")
        return
    if not trips:
        print("nothing pending — all trips already promoted.")
        return

    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = LOG_ROOT / f"overnight-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    total_gb = sum(_size_gb(t) for t in trips)
    cap = "  +captions" if args.captions else ""
    tc = "no-transcode" if args.no_transcode else "transcode"
    print(f"overnight: {len(trips)} trip(s), {total_gb:.0f} GB  ·  1 process ({tc}{cap}) "
          f"+ {args.promote_workers} upload streams  ·  logs → {run_dir}")
    print("Ctrl-C stops cleanly; re-run to resume.\n")

    # --- single process invocation over ALL trips (models loaded once) ---------
    proc_flags = ["--offline"]
    proc_flags.append("--no-transcode" if args.no_transcode else "--transcode")
    if args.captions:
        proc_flags.append("--with-captions")
    # Skip trips already processed (their marker exists) to resume fast.
    to_process = [t for t in trips if not _is_processed(t)]
    process_log = open(run_dir / "process.log", "wb")
    process_proc = None
    if to_process:
        cmd = ["caffeinate", "-dims", "nice", "-n", "5", IMMY, "process",
               *[str(t) for t in to_process], *proc_flags]
        process_log.write((" ".join(cmd) + "\n\n").encode())
        process_log.flush()
        process_proc = subprocess.Popen(
            cmd, stdout=process_log, stderr=subprocess.STDOUT, start_new_session=True)

    # --- parallel promote pool, fed as trips finish processing -----------------
    env = os.environ.copy()
    env["IMMY_RSYNC_SSH_OPTS"] = SSH_OPTS
    promoted: set[str] = set()
    submitted: set[str] = set()
    failed: dict[str, int] = {}
    lock = threading.Lock()
    stopping = threading.Event()
    live_procs: dict[int, subprocess.Popen] = {}

    def promote_one(trip: Path) -> None:
        if stopping.is_set():
            return
        log = run_dir / f"promote-{trip.name}.log"
        with open(log, "wb") as fh:
            # Apply HIGH findings (GPS-from-siblings, dates, trip-tags, …) +
            # auto-accept MEDIUM before promote — promote refuses on pending
            # HIGH. Same per-trip flow as promote-parallel/promote-all-trips.
            for stage in (["audit", str(trip), "--write", "--auto", "--yes-medium"],
                          ["promote", str(trip)]):
                if stopping.is_set():
                    return
                p = subprocess.Popen(
                    ["caffeinate", "-dims", "nice", "-n", "10", IMMY, *stage],
                    stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
                with lock:
                    live_procs[p.pid] = p
                p.wait()
                with lock:
                    live_procs.pop(p.pid, None)
                if p.returncode != 0:
                    with lock:
                        failed[trip.name] = p.returncode
                    return
            with lock:
                promoted.add(trip.name)

    def stop_all() -> None:
        stopping.set()
        with lock:
            procs = list(live_procs.values())
        if process_proc and process_proc.poll() is None:
            try:
                os.killpg(os.getpgid(process_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    signal.signal(signal.SIGINT, lambda *_: (print("\ninterrupt — stopping…"), stop_all()))

    run_start = time.time()
    with ThreadPoolExecutor(max_workers=args.promote_workers) as ex:
        futures = []
        while not stopping.is_set():
            # Hand every freshly-processed (or already-processed) trip to the pool.
            for t in trips:
                with lock:
                    done = t.name in submitted
                if done or _is_promoted(t):
                    continue
                if _is_processed(t):
                    with lock:
                        submitted.add(t.name)
                    futures.append(ex.submit(promote_one, t))
            proc_done = process_proc is None or process_proc.poll() is not None
            with lock:
                all_handled = all(t.name in submitted or _is_promoted(t) for t in trips)
            if proc_done and all_handled and all(f.done() for f in futures):
                break
            # status line
            with lock:
                ok, fail, inflight = len(promoted), len(failed), len(live_procs)
            pend = sum(1 for t in trips if t.name not in submitted and not _is_promoted(t))
            phase = "processing" if not proc_done else "draining uploads"
            el = int(time.time() - run_start)
            print(f"\r[{el//3600}h{(el%3600)//60:02d}m] {phase}  ·  "
                  f"uploaded {ok}  uploading {inflight}  waiting-on-process {pend}  "
                  f"failed {fail}   ", end="", flush=True)
            time.sleep(5)
        ex.shutdown(wait=not stopping.is_set())

    if process_proc and process_proc.poll() is None:
        try:
            process_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    process_log.close()
    print(f"\n\nDone. {len(promoted)} uploaded, {len(failed)} failed, "
          f"{len(trips)-len(promoted)-len(failed)} not finished.  logs {run_dir}")
    for name, rc in failed.items():
        print(f"  fail rc={rc}  {name}  → {run_dir}/promote-{name}.log")


if __name__ == "__main__":
    main()
