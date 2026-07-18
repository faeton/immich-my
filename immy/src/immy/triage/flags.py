"""Immich favorite/album flags for triage, straight from Postgres.

Read-only companion to `triage.engine.scan` — the same direct-PG channel
`process` writes through, because favorites/albums are the one signal a
human has already expressed inside Immich and the REST search-by-path
API needs one round-trip per asset (3.8k clips would crawl).

The lookup translates manifest paths (`/originals/...`) to Immich
`originalPath` values by swapping the manifest root for the library's
`importPaths[0]` (read live via `pg.fetch_library_info`, never hardcoded).
"""

from __future__ import annotations

from ..config import Config
from ..pg import connect, fetch_library_info

_CHUNK = 500


def build_immich_lookup(config: Config, manifest_root: str):
    """Return `lookup(paths) -> {path: (favorite, album_count)}` or None
    when the config lacks a pg/immich block (offline runs degrade to
    NULL flags rather than failing the scan)."""
    if config.pg is None or config.immich is None or not config.immich.library_id:
        return None

    root = manifest_root.rstrip("/")

    def lookup(paths: list[str]) -> dict[str, tuple[bool, int]]:
        out: dict[str, tuple[bool, int]] = {}
        with connect(config.pg) as conn:
            import_root = fetch_library_info(
                conn, config.immich.library_id
            ).container_root
            to_immich = {
                p: import_root + p[len(root):]
                for p in paths if p.startswith(root + "/")
            }
            immich_to_manifest = {v: k for k, v in to_immich.items()}
            immich_paths = list(to_immich.values())
            for i in range(0, len(immich_paths), _CHUNK):
                chunk = immich_paths[i:i + _CHUNK]
                rows = conn.execute(
                    'SELECT a."originalPath", a."isFavorite", '
                    '  (SELECT COUNT(*) FROM album_asset aa '
                    '   WHERE aa."assetId" = a.id) '
                    'FROM asset a WHERE a."originalPath" = ANY(%s)',
                    (chunk,),
                ).fetchall()
                for original_path, favorite, albums in rows:
                    manifest_path = immich_to_manifest.get(original_path)
                    if manifest_path:
                        out[manifest_path] = (bool(favorite), int(albums))
        return out

    return lookup
