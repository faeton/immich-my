#!/usr/bin/env python3
"""Threaded, throttlable bulk-promote — fill a fat-but-lossy uplink with
many concurrent rsync streams instead of one.

WHY THIS EXISTS
    `tools/promote-all-trips.sh` promotes trips one at a time. Over a
    direct-but-roaming Tailscale path (measured: 78 ms RTT, single TCP
    stream sustains ~60 Mbps then sawtooths to ~3 Mbps on loss), a single
    stream leaves a 500 Mbps line mostly idle. Independent streams don't
    share one stream's congestion collapse, so N concurrent trips multiply
    aggregate throughput. This launcher runs the same `immy audit` +
    `immy promote` per trip, but N at a time, biggest-first.

DESIGN (settled with a Codex + Grok review):
    - Pure rsync-over-SSH — no transport swap. The big trips are a handful
      of huge files, so concurrent *trips* already give independent streams.
    - LPT scheduling: sort biggest-first so a 300 GB trip never strands the
      tail of the run.
    - Throttle: --bwlimit-total is split evenly across workers via the
      IMMY_RSYNC_BWLIMIT hook in promote.py (a predictable hard ceiling).
    - ssh ControlMaster is force-disabled (it would multiplex every worker
      onto ONE TCP and erase the whole point); compression off; keepalives.
    - Resumable: skips trips already logged as promoted (unless --force);
      rsync --partial --inplace --append-verify makes a killed transfer
      resume safely.
    - Clean Ctrl-C: each worker's rsync runs in its own process group and
      is terminated on SIGINT (ThreadPoolExecutor alone orphans them).

USAGE
    tools/promote-parallel.py                      # all pending, -P 4, no throttle
    tools/promote-parallel.py -P 6                 # 6 concurrent trips
    tools/promote-parallel.py -P 6 --bwlimit-total 80m   # cap aggregate ~80 MiB/s
    tools/promote-parallel.py '2025-11-pacific-*'  # subset by glob
    tools/promote-parallel.py --status             # size + promoted/pending table
    tools/promote-parallel.py --dry-run            # rsync --dry-run every trip
    tools/promote-parallel.py --whole-file         # fresh huge-video runs (no delta)

ENV: TRIPS_ROOT (default ~/Media/Trips), IMMY_CONFIG (default ~/.immy/config.yml).
"""

from __future__ import annotations

import os
import sys

# --- Run under immy's venv so rich + pyyaml are importable ------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PY = os.path.join(_REPO, "immy", ".venv", "bin", "python")
try:
    import rich  # noqa: F401
    import yaml
except ImportError:
    if os.path.exists(_VENV_PY) and sys.executable != _VENV_PY and not os.environ.get("_PP_REEXEC"):
        os.environ["_PP_REEXEC"] = "1"
        os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:])
    raise

import argparse
import glob
import json
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

console = Console()

TRIPS_ROOT = Path(os.environ.get("TRIPS_ROOT", str(Path.home() / "Media" / "Trips")))
CONFIG_PATH = Path(os.environ.get("IMMY_CONFIG", str(Path.home() / ".immy" / "config.yml")))
LOG_ROOT = Path.home() / ".immy" / "promote-logs"

# ssh transport tuning shared by every worker. ControlMaster=no is the load-
# bearing one: with multiplexing on, all parallel rsyncs ride one TCP and we
# lose the multi-stream win entirely.
SSH_OPTS = ("-o ControlMaster=no -o ControlPath=none -o Compression=no "
            "-o IPQoS=throughput -o ServerAliveInterval=15 -o ServerAliveCountMax=3")

_RATE_RE = re.compile(r"([\d.]+)([kKMGT]?)B/s")
_PCT_RE = re.compile(r"\b(\d+)%")
_ITEMIZE_RE = re.compile(r"^[<>][fdL]")
_MULT = {"": 1 / (1024 * 1024), "k": 1 / 1024, "K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024 * 1024.0}


@dataclass
class TripState:
    name: str
    size_gb: float
    status: str = "queued"          # queued | audit | rsync | api | done | failed | skipped
    rate_mbs: float = 0.0           # EWMA MiB/s for the in-flight file
    pct: int = 0                    # current file %
    cur_file: str = ""
    started: float = 0.0
    finished: float = 0.0
    rc: int | None = None
    log: Path | None = None
    last_seen: float = 0.0          # wall time of last progress line


# --- trip discovery ---------------------------------------------------------

def _is_promoted(trip: Path) -> bool:
    audit = trip / ".audit" / "audit.jsonl"
    if not audit.is_file():
        return False
    try:
        for line in audit.read_text().splitlines():
            if '"promoted"' in line and '"event"' in line:
                return True
    except OSError:
        pass
    return False


def _dir_size_gb(trip: Path) -> float:
    try:
        out = subprocess.run(["du", "-sk", str(trip)], capture_output=True, text=True)
        kb = int(out.stdout.split()[0])
        return kb / (1024 * 1024)
    except (ValueError, IndexError, subprocess.SubprocessError):
        return 0.0


def discover(patterns: list[str], *, force: bool) -> list[TripState]:
    if patterns:
        dirs: list[Path] = []
        seen = set()
        for pat in patterns:
            matched = sorted(glob.glob(str(TRIPS_ROOT / pat)))
            hits = [Path(d) for d in matched if Path(d).is_dir()]
            if not hits:
                console.print(f"[red]no trips matched:[/red] {pat}")
                sys.exit(1)
            for d in hits:
                if d not in seen:
                    seen.add(d)
                    dirs.append(d)
    else:
        dirs = sorted(p for p in TRIPS_ROOT.iterdir()
                      if p.is_dir() and not p.name.startswith("."))

    trips: list[TripState] = []
    for d in dirs:
        promoted = _is_promoted(d)
        if promoted and not force:
            trips.append(TripState(name=d.name, size_gb=_dir_size_gb(d), status="skipped"))
        else:
            trips.append(TripState(name=d.name, size_gb=_dir_size_gb(d)))
    # LPT: biggest-first among the ones we'll actually run.
    trips.sort(key=lambda t: (t.status == "skipped", -t.size_gb))
    return trips


# --- preflight --------------------------------------------------------------

def _ssh_host_and_path() -> tuple[str, str] | None:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except OSError:
        return None
    root = cfg.get("originals_root")
    if isinstance(root, str) and ":" in root:
        host, path = root.split(":", 1)
        return host, path
    return None


def preflight() -> None:
    if not CONFIG_PATH.is_file():
        console.print(f"[red]config not found:[/red] {CONFIG_PATH}")
        sys.exit(1)
    hp = _ssh_host_and_path()
    if hp is None:
        console.print("[yellow]originals_root is local or unset — skipping ssh reachability check[/yellow]")
        return
    host, path = hp
    console.print(f"Reachability check: ssh [cyan]{host}[/cyan] …")
    rc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
         f"test -d '{path}'"],
    ).returncode
    if rc != 0:
        console.print(f"[red]ssh {host} unreachable or {path} missing[/red] — on the tailnet?")
        sys.exit(1)
    console.print(f"  [green]OK[/green] {host}:{path} reachable")

    # Advisory: macOS TCP send buffer vs the ~1 MB BDP at 78 ms / 100 Mbps.
    try:
        snd = int(subprocess.run(["sysctl", "-n", "net.inet.tcp.autosndbufmax"],
                                 capture_output=True, text=True).stdout.strip())
        if snd < 4 * 1024 * 1024:
            console.print(
                f"  [dim]tip: net.inet.tcp.autosndbufmax={snd} is small for this RTT. "
                "For steadier single-stream rate:\n"
                "    sudo sysctl -w net.inet.tcp.autosndbufmax=4194304 net.inet.tcp.autorcvbufmax=4194304[/dim]"
            )
    except (ValueError, subprocess.SubprocessError):
        pass


# --- worker -----------------------------------------------------------------

class Runner:
    def __init__(self, args, immy: str, run_dir: Path, per_worker_bwlimit: str | None):
        self.args = args
        self.immy = immy
        self.run_dir = run_dir
        self.bwlimit = per_worker_bwlimit
        self.live_procs: dict[int, subprocess.Popen] = {}
        self.proc_lock = threading.Lock()
        self.stopping = threading.Event()

    def _env(self) -> dict:
        env = os.environ.copy()
        env["IMMY_RSYNC_SSH_OPTS"] = SSH_OPTS
        if self.bwlimit:
            env["IMMY_RSYNC_BWLIMIT"] = self.bwlimit
        if self.args.whole_file:
            env["IMMY_RSYNC_WHOLE_FILE"] = "1"
        return env

    def _spawn(self, cmd: list[str], log_fh, env) -> subprocess.Popen:
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, start_new_session=True, bufsize=0,
        )
        with self.proc_lock:
            self.live_procs[p.pid] = p
        return p

    def _reap(self, p: subprocess.Popen) -> None:
        with self.proc_lock:
            self.live_procs.pop(p.pid, None)

    def stop_all(self) -> None:
        self.stopping.set()
        with self.proc_lock:
            procs = list(self.live_procs.values())
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 5
        for p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def _stream(self, p: subprocess.Popen, log_fh, st: TripState) -> None:
        """Pump child output to the per-trip log and parse rate/pct/file."""
        buf = b""
        while True:
            chunk = p.stdout.read(4096)
            if not chunk:
                break
            log_fh.write(chunk)
            log_fh.flush()
            buf += chunk
            # rsync overwrites the progress line with \r; split on both.
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()  # keep trailing partial
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = _RATE_RE.search(line)
                if m:
                    inst = float(m.group(1)) * _MULT[m.group(2)]
                    st.rate_mbs = inst if st.rate_mbs == 0 else 0.5 * st.rate_mbs + 0.5 * inst
                    pm = _PCT_RE.search(line)
                    if pm:
                        st.pct = int(pm.group(1))
                    st.last_seen = time.time()
                elif not _ITEMIZE_RE.match(line) and "/" in line and " " not in line.rstrip("/"):
                    # A bare path line = the file rsync just started.
                    st.cur_file = line.rsplit("/", 1)[-1]

    def run(self, st: TripState) -> None:
        if self.stopping.is_set():
            return
        trip = TRIPS_ROOT / st.name
        st.log = self.run_dir / f"{st.name}.log"
        st.started = time.time()
        env = self._env()
        wrap = ["caffeinate", "-dims", "nice", "-n", "10"]
        with open(st.log, "wb") as log_fh:
            try:
                if not self.args.no_audit and not self.args.dry_run:
                    st.status = "audit"
                    p = self._spawn(
                        wrap + [self.immy, "audit", str(trip),
                                "--write", "--auto", "--yes-medium"], log_fh, env)
                    self._stream(p, log_fh, st)
                    p.wait()
                    self._reap(p)
                    if p.returncode != 0:
                        st.status, st.rc, st.finished = "failed", p.returncode, time.time()
                        return
                    if self.stopping.is_set():
                        return

                st.status = "rsync"
                cmd = wrap + [self.immy, "promote", str(trip)]
                if self.args.dry_run:
                    cmd.append("--dry-run")
                p = self._spawn(cmd, log_fh, env)
                self._stream(p, log_fh, st)
                p.wait()
                self._reap(p)
                st.rc = p.returncode
                st.rate_mbs = 0.0
                st.status = "done" if p.returncode == 0 else "failed"
            except Exception as e:  # pragma: no cover - defensive
                log_fh.write(f"\n[launcher error] {e}\n".encode())
                st.status, st.rc = "failed", -1
            finally:
                st.finished = time.time()


# --- dashboard --------------------------------------------------------------

def _fmt_dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def render(trips: list[TripState], run_start: float, throttle: str | None) -> Table:
    active = [t for t in trips if t.status in ("audit", "rsync", "api")]
    done = [t for t in trips if t.status == "done"]
    failed = [t for t in trips if t.status == "failed"]
    queued = [t for t in trips if t.status == "queued"]
    skipped = [t for t in trips if t.status == "skipped"]
    agg = sum(t.rate_mbs for t in active)

    remaining_gb = sum(t.size_gb for t in trips if t.status in ("queued", "audit", "rsync", "api"))
    eta = (remaining_gb * 1024 / agg) if agg > 0.5 else 0

    table = Table(title=None, expand=True)
    table.add_column("trip", no_wrap=True)
    table.add_column("size", justify="right")
    table.add_column("phase")
    table.add_column("file %", justify="right")
    table.add_column("MiB/s", justify="right")
    table.add_column("current file", no_wrap=True)

    for t in sorted(active, key=lambda t: -t.rate_mbs):
        stale = "" if time.time() - t.last_seen < 30 else " [yellow]stale[/yellow]"
        table.add_row(t.name, f"{t.size_gb:.0f}G", t.status + stale,
                      f"{t.pct}%", f"{t.rate_mbs:.1f}",
                      (t.cur_file[:40] if t.cur_file else "…"))
    for t in queued[:3]:
        table.add_row(f"[dim]{t.name}[/dim]", f"[dim]{t.size_gb:.0f}G[/dim]",
                      "[dim]queued[/dim]", "", "", "")
    if len(queued) > 3:
        table.add_row(f"[dim]… +{len(queued) - 3} queued[/dim]", "", "", "", "", "")

    thr = f"  throttle {throttle}/worker" if throttle else ""
    table.caption = (
        f"[bold]{agg:.1f} MiB/s[/bold] aggregate ({agg * 8 / 1.024 / 1000:.0f} Mbps){thr}  ·  "
        f"{len(active)} running · [green]{len(done)} done[/green] · "
        f"[red]{len(failed)} failed[/red] · {len(queued)} queued · [dim]{len(skipped)} skipped[/dim]  ·  "
        f"run {_fmt_dur(time.time() - run_start)}"
        + (f"  ·  ~ETA {_fmt_dur(eta)}" if eta else "")
    )
    return table


# --- status mode ------------------------------------------------------------

def print_status(trips: list[TripState]) -> None:
    table = Table(title=f"{TRIPS_ROOT}", expand=True)
    table.add_column("trip", no_wrap=True)
    table.add_column("size", justify="right")
    table.add_column("status")
    pending_gb = 0.0
    for t in trips:
        if t.status == "skipped":
            table.add_row(f"[dim]{t.name}[/dim]", f"[dim]{t.size_gb:.1f}G[/dim]", "[dim]promoted[/dim]")
        else:
            pending_gb += t.size_gb
            table.add_row(t.name, f"{t.size_gb:.1f}G", "[yellow]pending[/yellow]")
    console.print(table)
    n_pending = sum(1 for t in trips if t.status != "skipped")
    console.print(f"\n[bold]{n_pending}[/bold] pending, "
                  f"[bold]{pending_gb:.1f} GB[/bold] ({pending_gb / 1024:.2f} TB)")


# --- bwlimit parsing --------------------------------------------------------

def per_worker_bwlimit(total: str | None, workers: int) -> str | None:
    """Split an aggregate budget across workers → rsync --bwlimit (KiB/s)."""
    if not total:
        return None
    m = re.fullmatch(r"([\d.]+)([kKmMgG]?)", total.strip())
    if not m:
        console.print(f"[red]bad --bwlimit-total:[/red] {total} (try 80m, 50000, 1g)")
        sys.exit(1)
    val = float(m.group(1))
    unit = m.group(2).lower()
    kib = val * {"": 1 / 1024, "k": 1, "m": 1024, "g": 1024 * 1024}[unit]
    each = max(8, int(kib // max(1, workers)))  # never below 8 KiB/s
    return str(each)


# --- main -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Threaded, throttlable bulk promote.")
    ap.add_argument("patterns", nargs="*", help="trip name(s) or glob(s); default: all pending")
    ap.add_argument("-P", "--parallel", type=int, default=4, help="concurrent trips (default 4)")
    ap.add_argument("--bwlimit-total", help="aggregate cap, split per worker (e.g. 80m, 50000)")
    ap.add_argument("--whole-file", action="store_true", help="skip rsync delta (fresh huge-video runs)")
    ap.add_argument("--force", action="store_true", help="redo trips already promoted")
    ap.add_argument("--dry-run", action="store_true", help="rsync --dry-run, no writes")
    ap.add_argument("--no-audit", action="store_true", help="skip the pre-promote audit pass")
    ap.add_argument("--status", action="store_true", help="print size/pending table and exit")
    args = ap.parse_args()

    if not TRIPS_ROOT.is_dir():
        console.print(f"[red]trips root not found:[/red] {TRIPS_ROOT}")
        sys.exit(1)

    if args.whole_file:
        supported = subprocess.run(["rsync", "--help"], capture_output=True, text=True)
        if "--whole-file" not in supported.stdout + supported.stderr:
            console.print("[yellow]--whole-file ignored:[/yellow] this rsync (openrsync) "
                          "doesn't support it. Install GNU rsync (brew) to use it.")

    immy = os.path.join(_REPO, "immy", ".venv", "bin", "immy")
    if not os.path.exists(immy):
        import shutil
        immy = shutil.which("immy") or immy

    console.print(f"[bold cyan]promote-parallel[/bold cyan]  scanning {TRIPS_ROOT} …")
    trips = discover(args.patterns, force=args.force)
    runnable = [t for t in trips if t.status != "skipped"]

    if args.status:
        print_status(trips)
        return
    if not runnable:
        console.print("[green]nothing pending[/green] — all matched trips already promoted.")
        return

    preflight()

    workers = max(1, min(args.parallel, len(runnable)))
    bwlimit = per_worker_bwlimit(args.bwlimit_total, workers)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = LOG_ROOT / f"parallel-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    total_gb = sum(t.size_gb for t in runnable)
    console.print(
        f"{len(runnable)} trip(s), {total_gb:.0f} GB ({total_gb / 1024:.2f} TB)  ·  "
        f"-P {workers}{'  ·  throttle ' + bwlimit + ' KiB/s/worker' if bwlimit else '  ·  no throttle'}  ·  "
        f"logs → {run_dir}")
    console.print("[dim]Ctrl-C stops cleanly (terminates each worker's rsync); re-run to resume.[/dim]\n")

    runner = Runner(args, immy, run_dir, bwlimit)
    run_start = time.time()

    def _sigint(signum, frame):
        console.print("\n[yellow]interrupt — stopping workers…[/yellow]")
        runner.stop_all()
    signal.signal(signal.SIGINT, _sigint)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(runner.run, t) for t in runnable]
        with Live(render(trips, run_start, bwlimit), console=console, refresh_per_second=4) as live:
            while any(not f.done() for f in futures):
                live.update(render(trips, run_start, bwlimit))
                time.sleep(0.5)
                if runner.stopping.is_set():
                    break
            live.update(render(trips, run_start, bwlimit))

    # --- summary ---
    done = [t for t in runnable if t.status == "done"]
    failed = [t for t in runnable if t.status == "failed"]
    console.print(
        f"\n[bold]Done.[/bold] [green]{len(done)} ok[/green], "
        f"[red]{len(failed)} failed[/red], "
        f"{len(runnable) - len(done) - len(failed)} not started.  "
        f"run {_fmt_dur(time.time() - run_start)}  ·  logs {run_dir}")
    for t in failed:
        console.print(f"  [red]fail rc={t.rc}[/red] {t.name}  → {t.log}")


if __name__ == "__main__":
    main()
