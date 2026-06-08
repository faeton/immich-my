"""In-place thumbnail/preview repair for assets already in Immich whose
derivatives are broken — scanned while their originals were still offline,
so Immich wrote a `__offline_placeholder__` thumbnail (or none) and never
regenerated after the files landed on the NAS.

Unlike delete+reingest, this REUSES each asset's existing Immich UUID, so
album membership, favorites, and face/person assignments survive. Per
broken asset:

1. resolve the asset's local source file under the Mac trip folder,
2. regenerate thumbnail + preview locally via `derivatives.compute_for_asset`
   (pyvips for images, ffmpeg poster for video) — on the laptop's cores,
   not the NAS,
3. rsync the staged derivatives into the NAS media tree,
4. UPSERT the `asset_file` rows (`ON CONFLICT DO UPDATE SET path`) so the
   placeholder path is replaced with the real one.

Idempotent: re-running regenerates and re-upserts; nothing is deleted.
Video `encoded_video` is intentionally skipped (transcode is a separate,
overnight-sized job) — only the broken thumbnail/preview are repaired.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import tempfile
import threading

from . import pg as pg_mod
from .config import Config
from .derivatives import compute_for_asset, staged_dir
from .promote import _INSERT_ASSET_FILE


# Broken = no thumbnail asset_file row at all, OR one pointing at the
# offline placeholder. Named params so the LIKE values never collide with
# psycopg's %-parameter parsing.
_BROKEN_SQL = """
SELECT a.id, a."originalPath", a.type
FROM asset a
WHERE a."originalPath" LIKE %(prefix)s
  AND (a."libraryId" = %(lib)s OR a."libraryId" IS NULL)
  AND a."deletedAt" IS NULL
  AND (
    NOT EXISTS (SELECT 1 FROM asset_file f
                WHERE f."assetId" = a.id AND f.type = 'thumbnail')
    OR EXISTS (SELECT 1 FROM asset_file f
               WHERE f."assetId" = a.id AND f.type = 'thumbnail'
               AND f.path LIKE %(ph)s)
  )
ORDER BY a."originalPath"
"""

_PLACEHOLDER = "%__offline_placeholder__%"


@dataclass
class BrokenAsset:
    asset_id: str
    original_path: str
    asset_type: str          # 'IMAGE' | 'VIDEO'
    source: Path             # resolved local file under the trip folder


@dataclass
class TripRepair:
    trip: str
    broken: int = 0
    missing_source: int = 0  # broken in DB but file not on the Mac
    generated: int = 0       # assets we staged derivatives for
    rows_upserted: int = 0
    status: str = "ok"       # ok | skipped | error
    detail: str = ""


def find_broken(conn, library_id: str, library, trip_name: str) -> list[tuple[str, str, str]]:
    prefix = f"{library.container_root.rstrip('/')}/{trip_name}/"
    with conn.cursor() as cur:
        cur.execute(_BROKEN_SQL, {"prefix": prefix + "%", "lib": library_id, "ph": _PLACEHOLDER})
        return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]


def _push_files(staged_root: Path, host_root: str, rel_paths: list[str]) -> None:
    """rsync ONLY the given relative paths to the NAS media tree.

    Deliberately NOT `_rsync_derivatives` (which pushes the whole
    `.audit/derivatives/` tree): that tree can hold stale derivatives from a
    prior `immy process` run — e.g. multi-MB `encoded-video/...` files, some
    under an `__offline_placeholder__` owner dir — which we must not re-upload.
    `--files-from` restricts the transfer to exactly what we just generated;
    rsync creates the needed parent dirs implicitly.
    """
    if not rel_paths:
        return
    src = f"{str(staged_root).rstrip('/')}/"
    dst = host_root if ":" in host_root else f"{host_root.rstrip('/')}/"
    with tempfile.NamedTemporaryFile("w", suffix=".lst", delete=False) as fh:
        fh.write("\n".join(rel_paths) + "\n")
        list_path = fh.name
    try:
        subprocess.run(
            ["rsync", "-rt", "--files-from", list_path, src, dst],
            check=True, capture_output=True, text=True,
        )
    finally:
        os.unlink(list_path)


def _resolve_source(original_path: str, container_root: str, trip_folder: Path) -> Path | None:
    """Map an Immich `originalPath` back to the local file under the Mac
    trip folder. Returns None if it doesn't fall under this trip or the
    file is absent (e.g. an Immich-only asset with no Mac source)."""
    prefix = f"{container_root.rstrip('/')}/{trip_folder.name}/"
    if not original_path.startswith(prefix):
        return None
    candidate = trip_folder / original_path[len(prefix):]
    return candidate if candidate.is_file() else None


def repair_trip(
    trip_folder: Path,
    config: Config,
    *,
    parallel: int = 8,
    dry_run: bool = False,
    progress=None,
) -> TripRepair:
    """Repair every broken-derivative asset under one trip folder."""
    result = TripRepair(trip=trip_folder.name)
    if config.pg is None or config.immich is None or config.media is None:
        result.status, result.detail = "skipped", "needs pg + immich + media config"
        return result

    conn = pg_mod.connect(config.pg)
    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
        rows = find_broken(conn, config.immich.library_id, library, trip_folder.name)
    finally:
        conn.close()

    result.broken = len(rows)
    if not rows:
        result.status = "ok"
        return result

    targets: list[BrokenAsset] = []
    for asset_id, original_path, asset_type in rows:
        src = _resolve_source(original_path, library.container_root, trip_folder)
        if src is None:
            result.missing_source += 1
            continue
        targets.append(BrokenAsset(asset_id, original_path, asset_type, src))

    if dry_run:
        result.generated = len(targets)
        result.detail = f"{len(targets)} would regenerate, {result.missing_source} no local source"
        return result

    # 1. Generate thumbnail+preview locally, in parallel (pyvips/ffmpeg
    #    release the GIL, so threads scale across cores).
    specs: list[dict] = []
    rel_paths: list[str] = []          # exactly the files to rsync this run
    specs_lock = threading.Lock()

    def _gen(t: BrokenAsset) -> None:
        res = compute_for_asset(
            source_media=t.source,
            asset_id=t.asset_id,
            owner_id=library.owner_id,
            asset_type=t.asset_type,
            trip_folder=trip_folder,
            transcode_videos=False,  # thumbnail repair only; no re-encode
        )
        keep = [df for df in res.files if df.kind in ("thumbnail", "preview")]
        rows_out = [
            {
                "asset_id": t.asset_id,
                "type": df.kind,
                "path": f"{config.media.container_root.rstrip('/')}/{df.relative_path}",
                "is_progressive": df.is_progressive,
                "is_transparent": df.is_transparent,
            }
            for df in keep
        ]
        with specs_lock:
            specs.extend(rows_out)
            rel_paths.extend(df.relative_path for df in keep)
            result.generated += 1
            if progress is not None:
                progress(result.generated, len(targets))

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futures = {ex.submit(_gen, t): t for t in targets}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:  # pragma: no cover - defensive per-asset
                errors.append(f"{futures[fut].source.name}: {e}")

    if not specs:
        result.status = "error" if errors else "ok"
        result.detail = ("; ".join(errors[:3]) if errors
                         else f"{result.missing_source} no local source")
        return result

    # 2. Push ONLY the derivatives we just generated (not the whole tree).
    try:
        _push_files(staged_dir(trip_folder), config.media.host_root, rel_paths)
    except Exception as e:
        result.status, result.detail = "error", f"rsync derivatives failed: {e}"
        return result

    # 3. Upsert asset_file rows → replace placeholder paths with the real ones.
    conn = pg_mod.connect(config.pg)
    try:
        with conn.cursor() as cur:
            for spec in specs:
                cur.execute(_INSERT_ASSET_FILE, spec)
        conn.commit()
        result.rows_upserted = len(specs)
    except Exception as e:
        conn.rollback()
        result.status, result.detail = "error", f"asset_file upsert failed: {e}"
        return result
    finally:
        conn.close()

    if errors:
        result.detail = f"{len(errors)} gen error(s): " + "; ".join(errors[:2])
    return result
