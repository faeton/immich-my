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

from .config import Config
from .exif import iter_media, read_folder
from .immich import ImmichClient, ImmichError, wait_for_asset
from .notes import notes_body, resolve as resolve_notes
from .process import is_processed as y_is_processed
from .rules import evaluate
from .state import State, log_event, patch_hash


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
