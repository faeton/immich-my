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

TWO-COMMAND OVERNIGHT (decoupled CPU vs internet — run both at once):
    # Terminal A — captions only (CPU/LM Studio, no network):
    tools/overnight.py --captions --reprocess --no-upload
    # Terminal B — sync only (uploads already-processed trips, drains caption cache):
    tools/overnight.py --no-process

    They're safe to run in parallel on the same trips: a caption written
    after a trip was already drained flips its offline-cache entry back to
    "unsynced", and B's drain (this run, or a quick re-run in the morning)
    pushes it. See immy/src/immy/offline.py (_mark_dirty / sync_trip guard).

ENV: TRIPS_ROOT (default ~/Media/Trips), IMMY_CONFIG (default ~/.immy/config.yml).
"""

from __future__ import annotations

import argparse
import atexit
import functools
import glob
import os
import shutil
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


CONFIG_PATH = Path(os.environ.get("IMMY_CONFIG", str(Path.home() / ".immy" / "config.yml")))


def _captioner_target() -> tuple[str, str | None]:
    """Resolve (endpoint, configured_model) the way `immy process` will:
    env override → config.yml ml.captioner → LM Studio default. model is
    None when nothing is pinned (immy then asks LM Studio what's loaded)."""
    endpoint = os.environ.get("IMMY_CAPTIONER_ENDPOINT")
    model = os.environ.get("IMMY_CAPTIONER_MODEL")
    if not (endpoint and model):
        try:
            import yaml
            cap = ((yaml.safe_load(CONFIG_PATH.read_text()) or {})
                   .get("ml", {}) or {}).get("captioner", {}) or {}
            endpoint = endpoint or cap.get("endpoint")
            model = model or cap.get("model")
        except Exception:
            pass
    return (endpoint or "http://localhost:1234/v1", model)


def _vlm_preflight() -> tuple[list[str], str]:
    """Probe the captioner endpoint. Returns (reasons, note): `reasons` are
    HARD blockers ([] == ok to run); `note` is a friendly one-line status.

    The point: a down VLM makes `immy process` print a cheerful 'Done' while
    writing zero captions (per-asset errors are skipped). But LM Studio does
    just-in-time loading — an unloaded-but-DOWNLOADED model loads on the first
    request — so "not currently loaded" is NOT a blocker. We gate on the model
    being present in the catalog (downloadable/loadable), not on `state`."""
    import json
    import urllib.request
    import urllib.error

    endpoint, want = _captioner_target()
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    # LM Studio's /api/v0/models lists every DOWNLOADED model with per-model
    # `state` (loaded vs not-loaded); /v1/models can't tell us either fact.
    url = f"{base.rstrip('/')}/api/v0/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            body = json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return ([f"VLM endpoint unreachable at {endpoint} ({e.__class__.__name__}: "
                 f"{getattr(e, 'reason', e)}). Captions cannot generate — "
                 f"start LM Studio (or your backend)."], "")
    items = body.get("data") or []
    available = {m.get("id") for m in items}            # downloaded → JIT-loadable
    loaded = {m.get("id") for m in items if m.get("state") == "loaded"}
    if not want:
        # No model pinned → immy uses whatever's loaded, else a fallback.
        if not available:
            return (["VLM reachable but NO models are installed — download a "
                     "VLM in LM Studio."], "")
        return ([], f"endpoint up, {len(available)} model(s) available "
                    f"(immy will use the loaded/auto-detected one).")
    if want not in available:
        return ([f"Configured captioner '{want}' is NOT downloaded in LM Studio "
                 f"(available: {', '.join(sorted(available)) or 'none'}). JIT can't "
                 f"load what isn't there — pull '{want}' or repoint "
                 f"ml.captioner.model in {CONFIG_PATH.name}."], "")
    if want in loaded:
        return ([], f"'{want}' is loaded and ready.")
    return ([], f"'{want}' is downloaded but not loaded — LM Studio will "
                f"JIT-load it on the first caption request.")


def _is_promoted(trip: Path) -> bool:
    audit = trip / ".audit" / "audit.jsonl"
    try:
        return audit.is_file() and '"promoted"' in audit.read_text()
    except OSError:
        return False


def _is_processed(trip: Path) -> bool:
    return (trip / ".audit" / "y_processed.yml").is_file()


@functools.lru_cache(maxsize=None)
def _size_gb(trip: Path) -> float:
    try:
        out = subprocess.run(["du", "-sk", str(trip)], capture_output=True, text=True)
        return int(out.stdout.split()[0]) / (1024 * 1024)
    except (ValueError, IndexError, subprocess.SubprocessError):
        return 0.0


def _read_heartbeat(trip: Path) -> dict | None:
    """Parse a trip's `.audit/.progress` flat-YAML heartbeat into a dict.

    Adds a synthetic `_age` (seconds since updated_at, from file mtime) so
    the dashboard can tell the live trip from stale leftovers."""
    p = trip / ".audit" / ".progress"
    try:
        text = p.read_text()
        mtime = p.stat().st_mtime
    except OSError:
        return None
    d: dict = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            d[k.strip()] = v.strip()
    d["_age"] = time.time() - mtime
    return d


def _offline_count(trip: Path) -> int:
    """Assets cached to the offline sink so far (one YAML per asset)."""
    try:
        return sum(1 for _ in (trip / ".audit" / "offline").glob("*.yml"))
    except OSError:
        return 0


def _last_rsync_line(path: Path, maxbytes: int = 8192) -> str:
    """Most recent rsync --progress line (`… 45%  2.34MB/s  0:00:05`)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - maxbytes))
            data = fh.read()
    except OSError:
        return ""
    best = ""
    for raw in data.decode("utf-8", "replace").splitlines():
        s = raw.strip()
        if "%" in s and ("B/s" in s or "/s" in s):
            best = s
    return best


def _pid_alive(pid) -> bool:
    """True if `pid` is a live process. Used to tell a heartbeat written by
    the running promote from a corpse left by an interrupted run (e.g. a
    killed `--captions` run that never reached `hb.clear()`)."""
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _trip_step(hb: dict | None, speed: str) -> str:
    """The step label to show for an active upload trip.

    The heartbeat is a single shared per-trip dotfile, so its `step` can be
    a lie: a corpse from a killed `--captions` run (dead pid), or — worse —
    a *live* concurrent `--captions` run writing `phase: process` into the
    same file. Trust the step only when it was written by a live *promote*
    writer; otherwise infer from what the upload log is actually doing."""
    if hb and hb.get("phase") == "promote" and _pid_alive(hb.get("pid")):
        return hb.get("step", "queued")
    return "rsync originals" if speed else "queued"


def _quiet_tty(is_tty: bool):
    """While the in-place dashboard owns the terminal, stop the kernel from
    injecting lines the repaint can't account for: Ctrl+T's SIGINFO `load: …`
    status line (NOKERNINFO) and the `^C`/`^T` control-char echo (ECHOCTL).
    Returns (fd, saved_attrs) to hand back to `_restore_tty`, or None."""
    if not is_tty:
        return None
    try:
        import termios
        fd = sys.stdout.fileno()
        saved = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~getattr(termios, "ECHOCTL", 0)
        attrs[3] |= getattr(termios, "NOKERNINFO", 0)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd, saved
    except Exception:
        return None


def _restore_tty(state) -> None:
    if not state:
        return
    try:
        import termios
        fd, saved = state
        termios.tcsetattr(fd, termios.TCSANOW, saved)
    except Exception:
        pass


class _ByteMeter:
    """Reconstruct bytes actually transferred this run. openrsync gives no
    cross-file byte total (no `to-chk`/`xfr#`), so we sum the on-disk sizes
    of the files it itemized as changed — the `<f…`/`>f…` lines emitted by
    `--itemize-changes`. Incremental: each log is read only past its last
    offset, each file stat-ed exactly once. Approximate (counts a file the
    moment rsync starts it, and skipped/unchanged files on a resumed run
    produce no itemize line), so callers prefix the figure with `~`."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._off: dict[Path, int] = {}
        self._buf: dict[Path, str] = {}   # trailing partial line per log
        self._by_trip: dict[str, int] = {}  # bytes moved, keyed by trip name
        self.bytes = 0
        self.files = 0

    def update(self, trip: Path, log: Path) -> None:
        try:
            with open(log, "rb") as fh:
                fh.seek(self._off.get(log, 0))
                data = fh.read()
                self._off[log] = fh.tell()
        except OSError:
            return
        # A mid-flush read can split a record; \r-progress updates become
        # their own (skipped) lines. Carry the unterminated tail to next poll
        # so a filename straddling a chunk boundary is never dropped.
        text = self._buf.get(log, "") + data.decode("utf-8", "replace")
        lines = text.replace("\r", "\n").split("\n")
        self._buf[log] = lines.pop()
        for raw in lines:
            parts = raw.split(None, 1)
            if len(parts) != 2:
                continue
            flags, rel = parts
            # A data-transferred regular file is `<f…` (sent) / `>f…` (recv).
            # This excludes dirs (`.d…`), symlinks (`<L…`), metadata-only
            # changes (`.f…`, no bytes moved), deletions, and progress/byte
            # lines (numeric first token). `.audit/derivatives` paths use a
            # different source root, so `trip / rel` won't resolve → dropped.
            if len(flags) < 2 or flags[0] not in "<>" or flags[1] != "f":
                continue
            key = f"{trip.name}/{rel}"
            if key in self._seen:
                continue
            self._seen.add(key)
            try:
                sz = (trip / rel).stat().st_size
                self.bytes += sz
                self._by_trip[trip.name] = self._by_trip.get(trip.name, 0) + sz
                self.files += 1
            except OSError:
                pass

    @property
    def gb(self) -> float:
        return self.bytes / (1024 ** 3)

    def trip_gb(self, name: str) -> float:
        return self._by_trip.get(name, 0) / (1024 ** 3)


def _last_line(path: Path, maxbytes: int = 8192) -> str:
    """Last non-empty line of a log — used to explain a failed promote."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - maxbytes))
            data = fh.read()
    except OSError:
        return ""
    for raw in reversed(data.decode("utf-8", "replace").splitlines()):
        if raw.strip():
            return raw.strip()
    return ""


class _ProcessLog:
    """Incremental tail of process.log: counts fresh VLM captions + assets
    seen and keeps the latest caption text, reading only new bytes per poll."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._off = 0
        self.attempted = 0   # `(VLM @ …)` lines: fresh VLM calls STARTED this run
        self.failed = 0      # `caption… FAILED:` lines (VLM down/errored)
        self.last_error = "" # most recent failure reason, for the dashboard
        self.assets = 0      # asset header lines seen
        self.last = ""       # most recent caption text
        self.recent: list[str] = []

    @property
    def captioned(self) -> int:
        """Captions actually generated this run = fresh calls that didn't
        fail. (Cached hits print `[cached, …]`, never `(VLM @`, so they're
        correctly excluded.) Never negative if a FAILED line outraces its
        attempt across a poll boundary."""
        return max(0, self.attempted - self.failed)

    def poll(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self._off)
                chunk = fh.read()
                self._off = fh.tell()
        except OSError:
            return
        if not chunk:
            return
        for raw in chunk.decode("utf-8", "replace").splitlines():
            s = raw.strip()
            if not s:
                continue
            self.recent.append(s)
            if s.startswith("[") and "]" in s and "/" in s.split("]", 1)[0]:
                self.assets += 1
            elif "caption… (VLM @" in s:
                self.attempted += 1
            elif "caption… FAILED:" in s:
                self.failed += 1
                self.last_error = s.split("FAILED:", 1)[1].strip()
            elif s.startswith("caption:") or 'caption: "' in s:
                snip = s.split('caption:', 1)[1].strip().strip('"').rstrip("…").strip('"')
                if snip:
                    self.last = snip
        self.recent = self.recent[-6:]


def discover(patterns: list[str], *, force: bool, smallest_first: bool = False) -> list[Path]:
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
    if smallest_first:
        # Upload-only: smallest first so trips actually COMPLETE early and
        # become queryable in Immich within minutes. Biggest-first here just
        # pins all N workers to the N giants for ~14 h while everything waits.
        pending.sort(key=_size_gb)
    else:
        # LPT: biggest first so a 300 GB trip's long process+upload starts early.
        pending.sort(key=lambda d: -_size_gb(d))
    return pending


def main() -> None:
    ap = argparse.ArgumentParser(description="Pipelined overnight process + parallel upload.")
    ap.add_argument("patterns", nargs="*", help="trip name(s)/glob(s); default: all pending")
    ap.add_argument("-P", "--promote-workers", type=int, default=4, help="concurrent upload streams (default 4)")
    ap.add_argument("--captions", action="store_true", help="also run the LM Studio VLM captioner (slow); fills only never-captioned assets by default")
    ap.add_argument("--recaption-all", action="store_true", help="with --captions: re-caption everything under the current model, even assets already captioned by a previous model (default: keep them)")
    ap.add_argument("--no-transcode", action="store_true", help="skip the 720p web transcode")
    ap.add_argument("--force", action="store_true", help="redo trips already promoted")
    ap.add_argument("--reprocess", action="store_true",
                    help="re-run `immy process --force` on every trip to fill missing CLIP/faces/captions (journal skips done phases), then upload")
    ap.add_argument("--no-upload", action="store_true",
                    help="CPU side only: run the process/caption stage, skip the upload pool (pairs with --captions --reprocess)")
    ap.add_argument("--no-process", action="store_true",
                    help="network side only: run the parallel upload pool over already-processed trips, skip the process stage")
    ap.add_argument("--status", action="store_true", help="print pending trips and exit")
    args = ap.parse_args()
    if args.no_upload and args.no_process:
        print("--no-upload and --no-process are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    if not TRIPS_ROOT.is_dir():
        print(f"trips root not found: {TRIPS_ROOT}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(IMMY):
        print(f"immy not found: {IMMY}", file=sys.stderr)
        sys.exit(1)

    # --reprocess backfills CLIP/faces/captions onto trips that are usually
    # ALREADY promoted, so discovery must include promoted trips too (else the
    # very trips needing caption backfill get filtered out).
    trips = discover(args.patterns, force=args.force or args.reprocess,
                     smallest_first=args.no_process)
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

    if args.no_process:
        # Upload side runs alone: a trip with no y_processed marker would
        # never become "ready", so the pool would wait on it forever. Drop
        # the unprocessed ones (the captions/process command handles those).
        ready_trips = [t for t in trips if _is_processed(t)]
        skipped = [t for t in trips if not _is_processed(t)]
        if skipped:
            print(f"--no-process: skipping {len(skipped)} not-yet-processed trip(s): "
                  + ", ".join(t.name for t in skipped))
        trips = ready_trips
        if not trips:
            print("nothing to upload — no processed trips pending.")
            return

    # Captions need a live, correctly-loaded VLM. Without this gate a
    # down/mismatched backend silently no-ops every caption yet the run
    # still ends "Done" — the exact confusion this guard exists to kill.
    if args.captions and not args.no_process:
        reasons, note = _vlm_preflight()
        if reasons:
            print("captioner preflight FAILED — captions would be skipped:")
            for r in reasons:
                print(f"  ✗ {r}")
            if os.environ.get("IMMY_SKIP_VLM_PREFLIGHT") == "1":
                print("  (IMMY_SKIP_VLM_PREFLIGHT=1 set — continuing anyway)\n")
            else:
                print("\nRefusing to start a caption run against a dead backend. "
                      "Fix the above, or set IMMY_SKIP_VLM_PREFLIGHT=1 to override.",
                      file=sys.stderr)
                sys.exit(2)
        else:
            print(f"captioner preflight OK — {note}")

    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = LOG_ROOT / f"overnight-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    sizes = {t.name: _size_gb(t) for t in trips}
    total_gb = sum(sizes.values())
    cap = "  +captions" if args.captions else ""
    tc = "no-transcode" if args.no_transcode else "transcode"
    if args.no_upload:
        mode = f"CPU only · 1 process ({tc}{cap}), no upload"
    elif args.no_process:
        mode = f"network only · {args.promote_workers} upload streams, no process"
    else:
        mode = f"1 process ({tc}{cap}) + {args.promote_workers} upload streams"
    print(f"overnight: {len(trips)} trip(s), {total_gb:.0f} GB  ·  {mode}  ·  logs → {run_dir}")
    print("Ctrl-C stops cleanly; re-run to resume.\n")

    # --- single process invocation over ALL trips (models loaded once) ---------
    proc_flags = ["--offline"]
    proc_flags.append("--no-transcode" if args.no_transcode else "--transcode")
    if args.captions:
        proc_flags.append("--with-captions")
        # Default: keep captions made by a previous model id, only fill the
        # never-captioned ones — a captioner-model bump shouldn't redo work
        # that's already good. `--recaption-all` opts into a full re-caption.
        if not args.recaption_all:
            proc_flags.append("--captions-fill-missing")
    if args.reprocess:
        # --force re-runs every trip; the per-asset journal still skips phases
        # already done, so this only fills the missing CLIP/faces/captions.
        proc_flags.append("--force")
        to_process = list(trips)
    else:
        # Default: only process trips with no marker yet (resume fast).
        to_process = [t for t in trips if not _is_processed(t)]
    # A trip is "ready to promote" once its marker is fresh. With --reprocess
    # the marker already exists, so gate on mtime >= when process started;
    # otherwise just on existence.
    process_start = time.time()

    def _ready(trip: Path) -> bool:
        m = trip / ".audit" / "y_processed.yml"
        if not m.is_file():
            return False
        return m.stat().st_mtime >= process_start if args.reprocess else True

    # PYTHONUNBUFFERED: child stdout is block-buffered when redirected to a
    # file, which would make the live tail lag by KBs. Force line-prompt
    # flushing so caption snippets show up in the dashboard as they land.
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"

    process_log_path = run_dir / "process.log"
    process_log = open(process_log_path, "wb")
    process_proc = None
    if to_process and not args.no_process:
        cmd = ["caffeinate", "-dims", "nice", "-n", "5", IMMY, "process",
               *[str(t) for t in to_process], *proc_flags]
        process_log.write((" ".join(cmd) + "\n\n").encode())
        process_log.flush()
        process_proc = subprocess.Popen(
            cmd, stdout=process_log, stderr=subprocess.STDOUT,
            env=child_env, start_new_session=True)
    plog = _ProcessLog(process_log_path)

    # --- parallel promote pool, fed as trips finish processing -----------------
    env = os.environ.copy()
    env["IMMY_RSYNC_SSH_OPTS"] = SSH_OPTS
    env["PYTHONUNBUFFERED"] = "1"
    promoted: set[str] = set()
    submitted: set[str] = set()
    failed: dict[str, int] = {}
    lock = threading.Lock()
    stopping = threading.Event()
    live_procs: dict[int, subprocess.Popen] = {}

    def _run(stage: list[str], fh) -> int:
        p = subprocess.Popen(
            ["caffeinate", "-dims", "nice", "-n", "10", IMMY, *stage],
            stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        with lock:
            live_procs[p.pid] = p
        p.wait()
        with lock:
            live_procs.pop(p.pid, None)
        return p.returncode

    def promote_one(trip: Path) -> None:
        if stopping.is_set():
            return
        log = run_dir / f"promote-{trip.name}.log"
        audit = ["audit", str(trip), "--write", "--auto", "--yes-medium"]
        with open(log, "wb") as fh:
            # Apply HIGH findings (GPS-from-siblings, dates, trip-tags, …) +
            # auto-accept MEDIUM, THEN promote. The catch: writing findings
            # shifts neighbour inference, so a re-read surfaces a fresh
            # cascade of HIGH findings a single pass never sees — promote
            # then refuses on those stragglers ("N HIGH pending"). So loop
            # audit→promote until promote accepts or the cascade stops
            # shrinking (genuinely-manual HIGH that auto can't apply → fail
            # loud after the cap, with the reason surfaced in the dashboard).
            for attempt in range(4):
                if stopping.is_set():
                    return
                if _run(audit, fh) != 0:
                    with lock:
                        failed[trip.name] = 1
                    return
                if stopping.is_set():
                    return
                rc = _run(["promote", str(trip)], fh)
                if rc == 0:
                    with lock:
                        promoted.add(trip.name)
                    return
                fh.write(f"\n[overnight] promote refused (rc={rc}); "
                         f"re-auditing for cascade (attempt {attempt + 1}/4)\n".encode())
                fh.flush()
            with lock:
                failed[trip.name] = rc

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
    is_tty = sys.stdout.isatty()
    prev_lines = 0          # rows the last dashboard frame occupied (TTY redraw)
    last_plain = 0.0        # throttle for non-TTY (nohup) append-only logging
    meter = _ByteMeter()    # bytes actually moved this run (upload side)
    gb_total = sum(_size_gb(t) for t in trips) if not args.no_upload else 0.0
    tty_state = _quiet_tty(is_tty)
    atexit.register(_restore_tty, tty_state)  # safety net if we exit abnormally
    with ThreadPoolExecutor(max_workers=args.promote_workers) as ex:
        futures = []
        while not stopping.is_set():
            # Hand every freshly-processed (or already-processed) trip to the
            # pool — unless this is a CPU-only (--no-upload) run.
            if not args.no_upload:
                for t in trips:
                    with lock:
                        done = t.name in submitted
                    if done or _is_promoted(t):
                        continue
                    if _ready(t):
                        with lock:
                            submitted.add(t.name)
                        futures.append(ex.submit(promote_one, t))
            proc_done = process_proc is None or process_proc.poll() is not None
            if args.no_upload:
                # No uploads to wait on; finish when the process stage exits.
                all_handled = True
            else:
                with lock:
                    all_handled = all(t.name in submitted or _is_promoted(t) for t in trips)
            if proc_done and all_handled and all(f.done() for f in futures):
                break
            # ---- live dashboard -------------------------------------------
            el = int(time.time() - run_start)
            stamp = f"{el//3600}h{(el % 3600)//60:02d}m{el % 60:02d}s"
            cols = shutil.get_terminal_size((100, 20)).columns
            lines: list[str] = []

            if args.no_upload:
                # CPU/captions side: heartbeat → which trip + [idx/total] +
                # phase; process.log tail → fresh-caption count + latest text.
                plog.poll()
                live = None  # (trip, hb) with the freshest heartbeat
                for t in to_process:
                    hb = _read_heartbeat(t)
                    if hb and (live is None or hb["_age"] < live[1]["_age"]):
                        live = (t, hb)
                cached = sum(_offline_count(t) for t in to_process)
                state = "running" if not proc_done else "done"
                fail_str = f"  ·  {plog.failed} FAILED" if plog.failed else ""
                lines.append(
                    f"[{stamp}] captions {state}  ·  {plog.captioned} generated{fail_str}  ·  "
                    f"{cached} assets cached  ·  {len(to_process)} trips")
                if plog.failed and plog.last_error:
                    # If captions are failing, the reason is the headline —
                    # not buried in a log nobody opens.
                    lines.append(f"    ✗ last caption error: {plog.last_error}")
                if live:
                    t, hb = live
                    age = int(hb.get("_age", 0))
                    idle = "  (idle — model warming / stuck?)" if age > 120 else ""
                    detail = " ".join(x for x in (hb.get("file", ""), hb.get("detail", "")) if x)
                    lines.append(
                        f"  ▸ {t.name}  [{hb.get('index','?')}/{hb.get('total','?')}]  "
                        f"{hb.get('step','')}  {detail}{idle}".rstrip())
                if plog.last:
                    lines.append(f"    last caption: “{plog.last}”")
            else:
                # Upload side: per active trip, heartbeat step + live rsync
                # speed; failed trips show their last log line so "failed 1"
                # is never a mystery.
                with lock:
                    ok, fail, inflight = len(promoted), len(failed), len(live_procs)
                    submitted_n = set(submitted)
                    promoted_n = set(promoted)
                    failed_n = dict(failed)
                pend = sum(1 for t in trips if t.name not in submitted_n and not _is_promoted(t))
                for t in trips:
                    if t.name in submitted_n:
                        meter.update(t, run_dir / f"promote-{t.name}.log")
                phase = "uploading" if not proc_done else "draining uploads"
                lines.append(
                    f"[{stamp}] {phase}  ·  {ok}/{len(trips)} done  ·  "
                    f"~{meter.gb:.0f}/{gb_total:.0f} GB  ·  {inflight} active  ·  "
                    f"{pend} waiting  ·  {fail} failed")
                active = [t for t in trips if t.name in submitted_n
                          and t.name not in promoted_n and t.name not in failed_n]
                for t in active[:max(1, args.promote_workers)]:
                    hb = _read_heartbeat(t)
                    speed = _last_rsync_line(run_dir / f"promote-{t.name}.log")
                    step = _trip_step(hb, speed)
                    line = f"  ▸ {t.name}  ·  {step}"
                    tot = sizes.get(t.name, 0)
                    if tot:
                        done_gb = meter.trip_gb(t.name)
                        pct = min(100, int(done_gb / tot * 100))
                        line += f"  ·  ~{done_gb:.1f}/{tot:.1f} GB ({pct}%)"
                    if speed:
                        line += f"  ·  {speed}"
                    lines.append(line)
                for name, rc in list(failed_n.items())[-3:]:
                    why = _last_line(run_dir / f"promote-{name}.log") or "see log"
                    lines.append(f"  ✗ {name}  rc={rc}  ·  {why}")

            lines = [ln if len(ln) < cols else ln[:cols - 1] for ln in lines]
            if is_tty:
                if prev_lines:
                    sys.stdout.write(f"\x1b[{prev_lines}F\x1b[J")  # up N, clear down
                sys.stdout.write("\n".join(lines) + "\n")
                prev_lines = len(lines)
                sys.stdout.flush()
            elif time.time() - last_plain >= 20:  # nohup/redirect: append-only
                print("  |  ".join(lines), flush=True)
                last_plain = time.time()
            time.sleep(2)
        ex.shutdown(wait=not stopping.is_set())
    _restore_tty(tty_state)  # back to normal echo for the final summary
    atexit.unregister(_restore_tty)  # normal path done; drop the fallback

    if process_proc and process_proc.poll() is None:
        try:
            process_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    process_log.close()
    if args.no_upload:
        rc = process_proc.poll() if process_proc else 0
        state = "completed" if rc == 0 else f"exited rc={rc}"
        plog.poll()  # drain any tail written after the loop's last poll
        print(f"\n\nDone (CPU only). process stage {state}.  logs {run_dir}")
        if args.captions:
            # "completed" only means the child exited — it says nothing about
            # whether captions were actually written. Report the real split.
            print(f"  captions: {plog.captioned} generated, {plog.failed} failed "
                  f"(of {plog.attempted} attempted).")
            if plog.failed:
                print(f"  ✗ {plog.failed} caption(s) failed — last reason: "
                      f"{plog.last_error or 'see process.log'}")
            if plog.failed and plog.captioned == 0:
                print("  ⚠ ZERO captions generated despite a clean exit — the "
                      "backend is down or misconfigured; nothing was captioned.")
    else:
        for t in trips:
            meter.update(t, run_dir / f"promote-{t.name}.log")
        print(f"\n\nDone. {len(promoted)} uploaded, {len(failed)} failed, "
              f"{len(trips)-len(promoted)-len(failed)} not finished.  "
              f"~{meter.gb:.0f} GB moved in {meter.files} files of {gb_total:.0f} GB total.  "
              f"logs {run_dir}")
    for name, rc in failed.items():
        why = _last_line(run_dir / f"promote-{name}.log")
        print(f"  fail rc={rc}  {name}  → {run_dir}/promote-{name}.log")
        if why:
            print(f"           {why}")


if __name__ == "__main__":
    main()
