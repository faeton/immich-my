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
import subprocess

from . import offline as offline_mod
from . import pg as pg_mod
from .config import Config
from .derivatives import DERIVATIVES_DIR
from .exif import iter_media, read_folder
from .immich import ImmichClient, ImmichError, wait_for_asset
from .notes import notes_body, resolve as resolve_notes
from .process import is_processed as y_is_processed, read_marker as y_read_marker
from .rules import evaluate
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
    findings = evaluate(rows, folder)

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
    args = ["rsync", "-a", "--itemize-changes", "--progress"]
    if dry_run:
        args.append("--dry-run")
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
    if tty_out is None or not is_tty:
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            # Surface rsync's own complaint — otherwise callers see only the
            # exit code and have to re-run manually to diagnose (we hit this
            # during the bolivia promote: exit 255 with no visible reason).
            sys.stderr.write(proc.stderr)
            sys.stderr.flush()
            raise subprocess.CalledProcessError(
                proc.returncode, args,
                output=proc.stdout, stderr=proc.stderr,
            )
        return proc

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0,
    )
    buf = bytearray()
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        buf.extend(chunk)
        tty_out.write(chunk)
        sys.stdout.flush()
    stderr = proc.stderr.read() if proc.stderr else b""
    rc = proc.wait()
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
) -> dict:
    """Run the plan. Returns a dict with summary counters; `promote` CLI
    formats it for the user. Separated from build_plan so tests can stub
    the client."""
    rsync_proc = rsync(plan.folder, plan.target, dry_run=dry_run)
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
    offline_summary = _drain_offline_cache(plan.folder, config, dry_run=dry_run)
    if offline_summary is not None:
        summary["offline_sync"] = offline_summary

    if dry_run:
        summary["stacks"] = [("planned", f"{p.lrv.name} ↔ {p.insv.name}") for p in plan.pairs]
        return summary

    # Record promote event in the source folder's audit log so the JSONL
    # log stays the single story of what happened to this trip.
    log_event(plan.folder, {
        "event": "promoted",
        "target": str(plan.target),
        "pair_count": len(plan.pairs),
    })

    if client is None or config.immich is None:
        return summary

    # Phase Y.1: if `immy process` already inserted rows for this trip, the
    # scan POST is pure wasted work — skip it. The marker is our signal.
    if y_is_processed(plan.folder):
        summary["scan_skipped_reason"] = "y_processed"
        derivatives_summary = _push_derivatives(plan, config)
        if derivatives_summary is not None:
            summary["derivatives"] = derivatives_summary
    else:
        try:
            client.scan_library(config.immich.library_id)
            summary["scan_triggered"] = True
        except ImmichError as e:
            summary["scan_error"] = str(e)
            return summary

    for pair in plan.pairs:
        summary["stacks"].append(_stack_pair(client, pair, plan.folder.name))

    summary["album"] = _sync_album(client, plan)

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


def _sync_album(client: ImmichClient, plan: Plan) -> dict:
    """Create or update an Immich album named after the trip folder.

    - Description comes from the notes body (below front-matter, with the
      `# Title` and scaffold hint stripped). Empty body → no description
      write.
    - Assets = every media file under the trip folder, resolved to Immich
      asset IDs via `POST /api/search/metadata?originalFileName=`. First
      file is polled (scan is async); subsequent files use a single lookup
      each, no wait, so large trips don't spend minutes per file.
    - Idempotent: `PUT /api/albums/{id}/assets` reports already-present
      assets as duplicates rather than failing.

    Returns a summary dict the CLI formats. Never raises — album sync is
    a nice-to-have, shouldn't block the rest of promote.
    """
    album_name = plan.folder.name
    summary: dict = {
        "name": album_name,
        "status": "skipped",
        "detail": "",
        "added": 0,
        "missing": 0,
    }

    notes = resolve_notes(plan.folder)
    description: str | None = None
    if notes is not None:
        body = notes_body(notes)
        description = body if body else None

    # Find or create the album.
    try:
        existing = client.find_album_by_name(album_name)
    except ImmichError as e:
        summary.update(status="error", detail=f"find album: {e}")
        return summary

    # Resolve asset IDs for every local media file. First file polls (scan
    # is async); the rest take one-shot lookups so the run doesn't spend
    # ~12 s per missing asset on a big trip.
    media_files = list(iter_media(plan.folder))
    asset_ids: list[str] = []
    missing = 0
    first = True
    folder_name = plan.folder.name
    for path in media_files:
        # Disambiguate by path suffix — sibling trip folders can share
        # filenames (e.g. a smoke-test dir with duplicates), and plain
        # filename lookup would grab the wrong asset.
        path_suffix = f"/{folder_name}/{path.name}"
        if first:
            aid = wait_for_asset(client, path.name, original_path_suffix=path_suffix)
            first = False
        else:
            try:
                aid = client.find_asset_id(path.name, original_path_suffix=path_suffix)
            except ImmichError:
                aid = None
        if aid:
            asset_ids.append(aid)
        else:
            missing += 1
    summary["missing"] = missing

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
