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

from . import pg as pg_mod
from .config import Config
from .derivatives import DERIVATIVES_DIR, THUMBS_SUBDIR
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
    an rsync-style `user@host:/path` string (handled transparently by rsync)."""
    args = ["rsync", "-av", "--itemize-changes"]
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
    return subprocess.run(args, capture_output=True, text=True, check=True)


def _stack_pair(client: ImmichClient, pair: InstaPair) -> tuple[str, str] | None:
    """Resolve both assets via search; return (status, message). `status` in
    {'stacked', 'skipped', 'error'} — caller logs/prints."""
    lrv_id = wait_for_asset(client, pair.lrv.name)
    insv_id = wait_for_asset(client, pair.insv.name)
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
        summary["stacks"].append(_stack_pair(client, pair))

    summary["album"] = _sync_album(client, plan)

    return summary


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
    """
    src = f"{str(src_root).rstrip('/')}/"
    dst = host_root if ":" in host_root else f"{host_root.rstrip('/')}/"
    args = ["rsync", "-a", "--itemize-changes", src, dst]
    return subprocess.run(args, capture_output=True, text=True, check=True)


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

    staged = plan.folder / AUDIT_DIR / DERIVATIVES_DIR / THUMBS_SUBDIR
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
    if not staged.is_dir():
        return {
            "status": "error",
            "detail": f"marker lists derivatives but {staged} is missing",
            "rows_written": 0,
        }

    try:
        _rsync_derivatives(staged.parent, config.media.host_root)
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
    for path in media_files:
        if first:
            aid = wait_for_asset(client, path.name)
            first = False
        else:
            try:
                aid = client.find_asset_id(path.name)
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
