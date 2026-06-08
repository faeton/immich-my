"""`immy promote` — rsync a trip folder into the Immich external-library
tree and trigger a scan. Stacks Insta360 `.insv` + `.lrv` pairs so each
shot collapses to one tile in the timeline.

Flow:

1. Guardrail: refuse if `immy audit` still has HIGH findings pending —
   we promote clean folders, not works-in-progress.
2. rsync `<folder>/` → `<originals_root>/<folder_name>/`. Exclude `.audit/`
   (machine state belongs on the Mac) and OS noise (`.DS_Store`, Spotlight).
3. If Immich creds are configured → `POST /api/libraries/{id}/scan`.
4. For each Insta360 pair recorded by `insta360-pair-by-ts-serial` →
   poll `search/metadata` until both asset IDs are indexed, then
   `POST /api/stacks` with `.lrv` as the primary.

`--dry-run` prints the plan and stops before any write or API call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import signal

from . import offline as offline_mod
from . import pg as pg_mod
from .config import Config
from .derivatives import DERIVATIVES_DIR
from .exif import read_folder
from .immich import ImmichClient, ImmichError, wait_for_asset
from .notes import notes_body, resolve as resolve_notes
from .process import is_processed as y_is_processed, read_marker as y_read_marker
from .rules import evaluate, dedup_by_field
from .heartbeat import Heartbeat
from .state import AUDIT_DIR, State, log_event, patch_hash


RSYNC_EXCLUDES = (
    ".audit/",
    ".audit/**",
    ".DS_Store",
    "._*",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    "Thumbs.db",
)


@dataclass
class InstaPair:
    insv: Path
    lrv: Path


@dataclass
class Plan:
    folder: Path
    target: Path
    pairs: list[InstaPair]
    pending_high: int


def build_plan(folder: Path, config: Config) -> Plan:
    if config.originals_root is None:
        raise RuntimeError(
            "originals_root not configured. Set `originals_root:` in "
            f"~/.immy/config.yml (or $IMMY_CONFIG)."
        )

    rows = read_folder(folder)
    state = State.load(folder)
    # Dedup the SAME way `immy audit` does (rules.dedup_by_field). Without
    # this, promote counted HIGH findings that audit deduplicates away — e.g.
    # `trip-gps-from-siblings` losing a file's GPS to an already-applied
    # `dji-gps-from-srt` — as "pending", then refused to promote a folder
    # audit considered clean. That stranded such trips as perpetually pending.
    findings = dedup_by_field(evaluate(rows, folder))

    pending_high = 0
    pairs_by_key: dict[tuple[str, str], InstaPair] = {}
    for f in findings:
        rel = f.path.relative_to(folder).as_posix()
        if f.action == "pair" and f.pair_with is not None:
            # Deduplicate: insta360 emits two findings per pair (one each way).
            key = tuple(sorted([rel, f.pair_with.relative_to(folder).as_posix()]))
            if key in pairs_by_key:
                continue
            if f.path.suffix.lower() == ".lrv":
                pair = InstaPair(lrv=f.path, insv=f.pair_with)
            else:
                pair = InstaPair(insv=f.path, lrv=f.pair_with)
            pairs_by_key[key] = pair
            continue
        if f.confidence != "high":
            continue
        ph = patch_hash({"action": f.action, "patch": f.patch, "pair_with": str(f.pair_with)})
        if not state.is_applied(rel, f.rule, ph):
            pending_high += 1

    target = config.originals_root / folder.name
    return Plan(
        folder=folder,
        target=target,
        pairs=list(pairs_by_key.values()),
        pending_high=pending_high,
    )


def rsync(folder: Path, target: Path, *, dry_run: bool) -> subprocess.CompletedProcess:
    """Copy folder contents into target. `target` may be a local path or
    an rsync-style `user@host:/path` string (handled transparently by rsync).

    Uses `--progress` (not `--info=progress2`) because macOS ships
    Apple's openrsync as `/usr/bin/rsync`, which rejects `--info=*`
    entirely. `--progress` gives per-file progress lines that both
    openrsync and GNU rsync understand. We stream stdout to the
    terminal so the `\r`-animated progress line renders, and collect
    the full output into `CompletedProcess.stdout` for the itemized-
    changes parser downstream.
    """
    # --partial keeps a half-copied file on interrupt; --inplace writes
    # straight to the destination path (no temp+rename), avoiding doubled
    # remote disk on a 50 GB file. We deliberately do NOT use plain
    # --append: it trusts the existing destination prefix without verifying
    # it, so a partial-but-different or size-matched-but-different file is
    # silently accepted and corrupted. --append-verify checksums the prefix
    # first, so we use it when the local rsync supports it (GNU rsync); on
    # Apple's openrsync (no --append-verify) we fall back to plain
    # --partial --inplace, whose delta-transfer checksum-verifies the
    # existing blocks rather than blindly trusting them.
    args = [
        "rsync", "-a", "--itemize-changes", "--progress",
        "--partial", "--inplace",
    ]
    if _rsync_supports("--append-verify"):
        args.append("--append-verify")
    # Opt-in tuning hooks. Unset → default promote is byte-for-byte unchanged;
    # tools/promote-parallel.py sets these per worker:
    #   IMMY_RSYNC_BWLIMIT    rsync --bwlimit syntax (e.g. "2m"). The parallel
    #                         launcher passes total_budget // worker_count, so
    #                         the aggregate is a predictable hard ceiling.
    #   IMMY_RSYNC_WHOLE_FILE skip delta-transfer on first copy of large
    #                         already-compressed media (the rolling checksum is
    #                         wasted CPU/IO when the dest doesn't exist yet).
    #   IMMY_RSYNC_SSH_OPTS   extra ssh options for the transport — disabling
    #                         ControlMaster (else parallel rsyncs multiplex onto
    #                         ONE TCP, killing the multi-stream win), compression
    #                         off, keepalives.
    bwlimit = os.environ.get("IMMY_RSYNC_BWLIMIT")
    if bwlimit:
        args.append(f"--bwlimit={bwlimit}")
    if os.environ.get("IMMY_RSYNC_WHOLE_FILE") and _rsync_supports("--whole-file"):
        # openrsync (macOS default) has no --whole-file; silently skip there.
        args.append("--whole-file")
    if dry_run:
        args.append("--dry-run")
    ssh_opts = os.environ.get("IMMY_RSYNC_SSH_OPTS")
    if ssh_opts:
        args.extend(["-e", f"ssh {ssh_opts}"])
    for pat in RSYNC_EXCLUDES:
        args.extend(["--exclude", pat])

    src = f"{str(folder).rstrip('/')}/"
    target_str = str(target)
    dst = target_str if ":" in target_str else f"{target_str.rstrip('/')}/"
    # Local-only: ensure parent exists so rsync doesn't fail on a missing
    # originals_root. Remote destinations (`user@host:/path`) are the
    # caller's setup problem — we'd need ssh to mkdir remotely.
    if ":" not in target_str:
        target.parent.mkdir(parents=True, exist_ok=True)
    args.extend([src, dst])
    return _run_streaming(args)


_RSYNC_FLAG_CACHE: dict[str, bool] = {}


def _rsync_supports(flag: str) -> bool:
    """True if the local rsync advertises `flag` in its --help output.

    openrsync (macOS default) and GNU rsync differ in supported flags;
    cached so we probe at most once per process.
    """
    cached = _RSYNC_FLAG_CACHE.get(flag)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            ["rsync", "--help"], capture_output=True, text=True,
        )
        help_text = (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        help_text = ""
    supported = flag in help_text
    _RSYNC_FLAG_CACHE[flag] = supported
    return supported


def _run_streaming(args: list[str]) -> subprocess.CompletedProcess:
    """Spawn rsync, tee stdout to the terminal AND capture it.

    rsync's `--progress` output uses `\r` to overwrite a single
    progress line — piping through Python would lose that animation,
    so we let the tty see raw bytes when attached. We still buffer the
    full output so the returned `CompletedProcess.stdout` matches what
    a `capture_output=True` call would have produced (callers parse
    the itemized-changes tail from it). When stdout isn't a real tty
    (e.g. Typer's CliRunner capture in tests), we fall back to plain
    `subprocess.run` — no progress, but tests stay deterministic.
    """
    import sys

    tty_out = getattr(sys.stdout, "buffer", None)
    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    if tty_out is None:
        # No buffered stdout (rare — pytest capture, etc.). Fall back to
        # a buffered run; callers see output only on completion.
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 20 or proc.returncode == -signal.SIGINT:
            raise KeyboardInterrupt
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            sys.stderr.flush()
            raise subprocess.CalledProcessError(
                proc.returncode, args,
                output=proc.stdout, stderr=proc.stderr,
            )
        return proc

    # Stream in both tty and pipe cases. When piped (e.g. `... | tee log`),
    # convert rsync's `\r` progress overwrites to newlines so the wrapping
    # process sees live progress instead of silence for the whole transfer.
    import threading

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0,
    )
    buf = bytearray()
    assert proc.stdout is not None
    # Drain stderr on a separate thread. We read stdout one byte at a time to
    # preserve rsync's `\r` progress animation, so a noisy stderr stream would
    # otherwise fill its 64 KiB pipe buffer and deadlock the child before we
    # ever reach `proc.stderr.read()`.
    stderr_buf = bytearray()

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            stderr_buf.extend(chunk)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()
    try:
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            buf.extend(chunk)
            out_chunk = chunk if is_tty else (b"\n" if chunk == b"\r" else chunk)
            tty_out.write(out_chunk)
            sys.stdout.flush()
        rc = proc.wait()
        stderr_thread.join()
        stderr = bytes(stderr_buf)
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise
    # rsync returns 20 when it was interrupted by SIGINT/SIGTERM. Treat it
    # as user cancellation so callers don't print a Python traceback or run
    # later promote phases after a partial transfer.
    if rc == 20 or rc == -signal.SIGINT:
        raise KeyboardInterrupt
    if rc != 0:
        raise subprocess.CalledProcessError(
            rc, args, output=bytes(buf), stderr=stderr,
        )
    return subprocess.CompletedProcess(
        args, rc,
        stdout=bytes(buf).decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _stack_pair(client: ImmichClient, pair: InstaPair, folder_name: str) -> tuple[str, str] | None:
    """Resolve both assets via search; return (status, message). `status` in
    {'stacked', 'skipped', 'error'} — caller logs/prints."""
    lrv_suffix = f"/{folder_name}/{pair.lrv.name}"
    insv_suffix = f"/{folder_name}/{pair.insv.name}"
    lrv_id = wait_for_asset(client, pair.lrv.name, original_path_suffix=lrv_suffix)
    insv_id = wait_for_asset(client, pair.insv.name, original_path_suffix=insv_suffix)
    if not lrv_id or not insv_id:
        missing = []
        if not lrv_id: missing.append(pair.lrv.name)
        if not insv_id: missing.append(pair.insv.name)
        return ("skipped", f"asset(s) not yet indexed: {', '.join(missing)}")
    try:
        client.create_stack(primary_asset_id=lrv_id, other_asset_ids=[insv_id])
    except ImmichError as e:
        return ("error", str(e))
    return ("stacked", f"{pair.lrv.name} primary, {pair.insv.name} child")


def execute(
    plan: Plan,
    config: Config,
    *,
    dry_run: bool,
    client: ImmichClient | None = None,
    resurrect_deleted: bool = False,
) -> dict:
    """Run the plan. Returns a dict with summary counters; `promote` CLI
    formats it for the user. Separated from build_plan so tests can stub
    the client."""
    hb = Heartbeat.for_trip(plan.folder, phase="promote")
    try:
        hb.write(step="rsync originals", detail=str(plan.target))
        rsync_proc = rsync(plan.folder, plan.target, dry_run=dry_run)
    except KeyboardInterrupt:
        hb.clear()
        raise
    rsync_changes = [
        line for line in rsync_proc.stdout.splitlines()
        if line and not line.startswith(("sending ", "total ", "sent "))
    ]

    summary: dict = {
        "target": str(plan.target),
        "rsync_dry_run": dry_run,
        "rsync_changes": rsync_changes,
        "scan_triggered": False,
        "stacks": [],  # list of (status, detail)
    }

    # Offline-cache drain: if this trip was processed with `immy process
    # --offline`, cached per-asset YAMLs are sitting in `.audit/offline/`
    # waiting to hit Postgres. Promote is the first moment the user
    # *must* be on the tailnet (rsync to NAS), so it's also the right
    # moment to flush the cache — otherwise a later library scan would
    # see files on disk with no DB rows and ingest them as blank assets.
    hb.write(step="offline cache drain")
    offline_summary = _drain_offline_cache(plan.folder, config, dry_run=dry_run)
    if offline_summary is not None:
        summary["offline_sync"] = offline_summary

    if dry_run:
        summary["stacks"] = [("planned", f"{p.lrv.name} ↔ {p.insv.name}") for p in plan.pairs]
        hb.clear()
        return summary

    # Record promote event in the source folder's audit log so the JSONL
    # log stays the single story of what happened to this trip.
    log_event(plan.folder, {
        "event": "promoted",
        "target": str(plan.target),
        "pair_count": len(plan.pairs),
    })

    if client is None or config.immich is None:
        hb.clear()
        return summary

    # Phase Y.1: if `immy process` already inserted rows for this trip, the
    # scan POST is pure wasted work — skip it. The marker is our signal.
    if y_is_processed(plan.folder):
        summary["scan_skipped_reason"] = "y_processed"
        hb.write(step="rsync derivatives")
        derivatives_summary = _push_derivatives(plan, config)
        if derivatives_summary is not None:
            summary["derivatives"] = derivatives_summary
    else:
        hb.write(step="library scan")
        try:
            client.scan_library(config.immich.library_id)
            summary["scan_triggered"] = True
        except ImmichError as e:
            summary["scan_error"] = str(e)
            hb.clear()
            return summary

    if plan.pairs:
        hb.write(step="stack insta360 pairs", total=len(plan.pairs))
    for idx, pair in enumerate(plan.pairs, start=1):
        hb.write(file=pair.lrv.name, index=idx)
        summary["stacks"].append(_stack_pair(client, pair, plan.folder.name))

    hb.write(step="album sync")
    summary["album"] = _sync_album(
        client, plan, config, resurrect_deleted=resurrect_deleted,
    )

    hb.clear()
    return summary


# --- Offline cache drain -------------------------------------------------


def _drain_offline_cache(
    folder: Path, config: Config, *, dry_run: bool,
) -> dict | None:
    """Flush any `.audit/offline/*.yml` entries produced by `process
    --offline` into Postgres. Runs before scan/stack/album so the DB is
    self-consistent by the time Immich sees the new files.

    Returns None when nothing to do, else a summary dict with counts and
    an optional `error` key. Failures are soft (we return the error in
    the summary rather than raising) — the caller is promote, and
    refusing to rsync because a few sync entries failed would block
    the path we actually need, NAS file upload.
    """
    entries = list(offline_mod.iter_entries(folder))
    if not entries:
        return None
    pending = sum(1 for _, e in entries if not e.get("synced"))
    if pending == 0:
        return {"total": len(entries), "pending": 0, "synced": 0, "failed": 0}

    if dry_run:
        return {
            "total": len(entries), "pending": pending,
            "synced": 0, "failed": 0, "note": "dry-run — skipped",
        }

    if config.pg is None or config.immich is None:
        return {
            "total": len(entries), "pending": pending,
            "synced": 0, "failed": 0,
            "error": "sync needs pg: and immich.library_id in config — skipped",
        }

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        return {
            "total": len(entries), "pending": pending,
            "synced": 0, "failed": 0,
            "error": f"pg connect failed: {e}",
        }

    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
    except LookupError as e:
        conn.close()
        return {
            "total": len(entries), "pending": pending,
            "synced": 0, "failed": 0, "error": str(e),
        }
    offline_mod.cache_library_info(library)

    try:
        result = offline_mod.sync_trip(folder, conn, library=library)
    finally:
        if not conn.closed:
            conn.close()
    return {
        "total": result["total"], "pending": pending,
        "synced": result["synced"], "failed": result["failed"],
    }


# --- Phase Y.2: derivative rsync + asset_file INSERT ----------------------


_INSERT_ASSET_FILE = """
INSERT INTO asset_file (
  "assetId", type, path, "isEdited", "isProgressive", "isTransparent"
) VALUES (
  %(asset_id)s, %(type)s, %(path)s, false,
  %(is_progressive)s, %(is_transparent)s
)
ON CONFLICT ("assetId", type, "isEdited") DO UPDATE
SET path = EXCLUDED.path,
    "isProgressive" = EXCLUDED."isProgressive",
    "isTransparent" = EXCLUDED."isTransparent"
"""


def _rsync_derivatives(src_root: Path, host_root: str) -> subprocess.CompletedProcess:
    """Push the staged `thumbs/` tree into `<host_root>/thumbs/`.

    `src_root` = `<trip>/.audit/derivatives/` (the `thumbs/` dir sits
    directly inside it). `host_root` may be local (`/volume1/...` over
    SMB) or remote (`user@host:/volume1/...`). Missing destination
    parents are the caller's setup — matches `rsync()` above.

    Uses `-rt` instead of `-a` so we don't try to chmod/chown existing
    destination dirs: Immich's container runs as root and pre-creates
    `thumbs/<userId>/` as root:root with drwxrwxrwx. We just need new
    files to land there; preserving source perms would ask the server
    to chmod a root-owned dir and fail with EPERM. Modes for newly
    created files fall back to the user's umask (fine — Immich reads).
    """
    src = f"{str(src_root).rstrip('/')}/"
    dst = host_root if ":" in host_root else f"{host_root.rstrip('/')}/"
    args = [
        "rsync", "-rt", "--itemize-changes", "--progress",
        # `_posters/` is our local scratch (video poster JPEGs we feed
        # to pyvips); Immich never reads it, so don't pollute the NAS.
        "--exclude", "_posters/", "--exclude", "_posters/**",
        src, dst,
    ]
    return _run_streaming(args)


def _push_derivatives(plan: Plan, config: Config) -> dict | None:
    """Rsync `.audit/derivatives/` into NAS media root + INSERT `asset_file`
    rows for every staged derivative the marker records.

    Returns a summary dict, or None when nothing to do. Never raises —
    failures are caught and surfaced via the returned `status`.
    """
    marker = y_read_marker(plan.folder)
    if not marker:
        return None
    if config.media is None or config.pg is None:
        return {
            "status": "skipped",
            "detail": "media: or pg: block missing in immy config",
            "rows_written": 0,
        }

    staged_root = plan.folder / AUDIT_DIR / DERIVATIVES_DIR
    file_specs: list[dict] = []
    for asset in marker.get("assets") or []:
        derivs = asset.get("derivatives") or []
        for d in derivs:
            file_specs.append({
                "asset_id": asset["id"],
                "type": d["kind"],
                "path": f"{config.media.container_root}/{d['relative_path']}",
                "is_progressive": bool(d.get("is_progressive")),
                "is_transparent": bool(d.get("is_transparent")),
            })

    if not file_specs:
        return {"status": "empty", "detail": "no derivatives in marker", "rows_written": 0}
    if not staged_root.is_dir():
        return {
            "status": "error",
            "detail": f"marker lists derivatives but {staged_root} is missing",
            "rows_written": 0,
        }

    try:
        _rsync_derivatives(staged_root, config.media.host_root)
    except subprocess.CalledProcessError as e:
        return {
            "status": "error",
            "detail": f"rsync derivatives failed: {e.stderr.strip() or e.stdout.strip()}",
            "rows_written": 0,
        }

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        return {
            "status": "error",
            "detail": f"pg connect failed: {e}",
            "rows_written": 0,
        }
    try:
        with conn.cursor() as cur:
            for spec in file_specs:
                cur.execute(_INSERT_ASSET_FILE, spec)
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {
            "status": "error",
            "detail": f"asset_file insert failed: {e}",
            "rows_written": 0,
        }
    finally:
        conn.close()

    return {
        "status": "pushed",
        "detail": f"{len(file_specs)} asset_file row(s) upserted",
        "rows_written": len(file_specs),
    }


def _sync_album(
    client: ImmichClient, plan: Plan, config: Config,
    *, resurrect_deleted: bool = False,
) -> dict:
    """Create or update an Immich album named after the trip folder.

    Resolves the asset list directly from Postgres via
    `originalPath LIKE '<container_root>/<trip>/%'`. The earlier
    implementation walked the local trip folder and looked each file up
    via `/api/search/metadata?originalFileName=` — that path silently
    drops anything Immich's library scanner has flagged `isOffline=true`
    or `deletedAt` (search hides soft-deleted rows). After a path rename
    or a transient rsync hiccup that path would leave 100+ assets out of
    the album with no warning. The DB query is authoritative and a few
    orders of magnitude faster than per-file HTTP round-trips.

    Side effect: clears `isOffline` for rows under this path whose files
    are back on disk. The Immich scanner only marks offline; it never
    auto-clears the flag, so a one-shot UPDATE here keeps the trip's view
    consistent with reality. By default we do NOT touch `deletedAt`:
    clearing it would resurrect assets the user soft-deleted in the Immich
    UI, silently undoing an explicit action. Pass `resurrect_deleted=True`
    (CLI `--resurrect-deleted`) to also un-delete rows under this path.

    Idempotent: `PUT /api/albums/{id}/assets` reports already-present
    assets as duplicates rather than failing. Never raises — album sync
    is a nice-to-have, shouldn't block the rest of promote.
    """
    album_name = plan.folder.name
    summary: dict = {
        "name": album_name,
        "status": "skipped",
        "detail": "",
        "added": 0,
        "resurrected": 0,
        "thumbs_repaired": 0,
    }

    notes = resolve_notes(plan.folder)
    description: str | None = None
    if notes is not None:
        body = notes_body(notes)
        description = body if body else None

    if config.pg is None or config.immich is None:
        summary.update(detail="pg/immich config missing — album sync skipped")
        return summary

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        summary.update(status="error", detail=f"pg connect failed: {e}")
        return summary

    asset_ids: list[str] = []
    repair_ids: list[str] = []
    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
        prefix = f"{library.container_root.rstrip('/')}/{album_name}/"
        like = prefix + "%"
        with conn.cursor() as cur:
            # Assets that need a thumbnail (re)generation: registered while
            # their original was still offline, so Immich wrote a
            # `__offline_placeholder__` thumb (or none) and never re-ran the
            # job after the file landed. Captured BEFORE the UPDATE below
            # clears isOffline. Clearing the flag alone doesn't re-queue
            # derivative jobs, so without this the trip shows "Error loading
            # image" on every tile forever.
            cur.execute(
                'SELECT a.id FROM asset a '
                'WHERE a."originalPath" LIKE %s '
                'AND (a."libraryId" = %s OR a."libraryId" IS NULL) '
                'AND a."deletedAt" IS NULL AND ('
                '  a."isOffline" = true '
                '  OR NOT EXISTS (SELECT 1 FROM asset_file f '
                '       WHERE f."assetId" = a.id AND f.type = %s) '
                '  OR EXISTS (SELECT 1 FROM asset_file f '
                '       WHERE f."assetId" = a.id AND f.type = %s '
                '       AND f.path LIKE %s))',
                (like, config.immich.library_id, "thumbnail", "thumbnail",
                 "%__offline_placeholder__%"),
            )
            repair_ids = [str(r[0]) for r in cur.fetchall()]
            if resurrect_deleted:
                cur.execute(
                    'UPDATE asset SET "isOffline" = false, "deletedAt" = NULL '
                    'WHERE "originalPath" LIKE %s '
                    'AND ("libraryId" = %s OR "libraryId" IS NULL) '
                    'AND ("isOffline" = true OR "deletedAt" IS NOT NULL)',
                    (like, config.immich.library_id),
                )
            else:
                # Default: only clear the scanner's offline flag. Leave
                # user-soft-deleted rows (deletedAt) alone.
                cur.execute(
                    'UPDATE asset SET "isOffline" = false '
                    'WHERE "originalPath" LIKE %s '
                    'AND ("libraryId" = %s OR "libraryId" IS NULL) '
                    'AND "isOffline" = true',
                    (like, config.immich.library_id),
                )
            summary["resurrected"] = cur.rowcount
            cur.execute(
                'SELECT id FROM asset '
                'WHERE "originalPath" LIKE %s '
                'AND ("libraryId" = %s OR "libraryId" IS NULL) '
                'AND "deletedAt" IS NULL '
                'ORDER BY "originalPath"',
                (like, config.immich.library_id),
            )
            asset_ids = [str(r[0]) for r in cur.fetchall()]
        conn.commit()
    except Exception as e:
        conn.rollback()
        summary.update(status="error", detail=f"pg query failed: {e}")
        return summary
    finally:
        conn.close()

    # Repair broken thumbnails for assets brought back online by this rsync.
    # Soft: a job-queue failure must not fail the promote (files are already
    # on the NAS). Immich regenerates async in the background.
    if repair_ids:
        try:
            client.regenerate_thumbnails(repair_ids)
            summary["thumbs_repaired"] = len(repair_ids)
        except ImmichError as e:
            summary["thumbs_repair_error"] = str(e)

    try:
        existing = client.find_album_by_name(album_name)
    except ImmichError as e:
        summary.update(status="error", detail=f"find album: {e}")
        return summary

    try:
        if existing is None:
            album_id = client.create_album(
                album_name,
                description=description,
                asset_ids=asset_ids,
            )
            if album_id is None:
                summary.update(status="error", detail="create returned no id")
                return summary
            summary.update(
                status="created",
                detail=f"id={album_id}; {len(asset_ids)} asset(s)",
                added=len(asset_ids),
            )
            return summary

        album_id = existing.get("id")
        if album_id is None:
            summary.update(status="error", detail="existing album has no id")
            return summary

        if description is not None and existing.get("description") != description:
            client.update_album(album_id, description=description)
        results = client.add_assets_to_album(album_id, asset_ids)
        added = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        summary.update(
            status="updated",
            detail=f"id={album_id}; {added}/{len(asset_ids)} new",
            added=added,
        )
    except ImmichError as e:
        summary.update(status="error", detail=str(e))
    return summary
