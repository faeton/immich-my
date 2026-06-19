"""Resolver for every path `immy process` WRITES to.

Historically all process state (journal, marker, heartbeat, staged
derivatives, offline cache) lived under `<trip>/.audit/`, and sidecars
(`.srt`, `.xmp`) were written next to the media. That breaks when the
originals are a READ-ONLY mount — the NAS case (Phase 6), where immy
enriches the live Immich external library it must never mutate.

`resolve_writable_paths` returns a `WritablePaths` that, when both new
roots are unset, reproduces the historical layout BYTE-IDENTICALLY (the
Mac path is unchanged). When `state_root` / `sidecars_root` are set (NAS),
state goes to `state_root/<trip-rel>/.audit/...` and sidecars mirror the
media layout under `sidecars_root/<trip-rel>/...`, leaving originals `:ro`.

`<trip-rel>` is the trip folder relative to `originals_root` when given,
else the trip folder's own name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .heartbeat import HEARTBEAT_FILENAME
from .journal import JOURNAL_FILENAME
from .state import AUDIT_DIR, Y_MARKER_FILENAME


@dataclass(frozen=True)
class WritablePaths:
    """Resolved write targets for one trip. Construct via
    `resolve_writable_paths` — never instantiate directly."""

    audit_dir: Path
    marker_path: Path
    journal_path: Path
    heartbeat_path: Path
    derivatives_dir: Path
    offline_dir: Path
    # Inputs for media-relative sidecar resolution. `sidecars_dir is None`
    # means "write sidecars next to the media" (the Mac default).
    trip_folder: Path
    sidecars_dir: Path | None

    def _sidecar_dir_for(self, media: Path) -> Path:
        if self.sidecars_dir is None:
            return media.parent
        try:
            rel = media.relative_to(self.trip_folder)
        except ValueError:
            rel = Path(media.name)
        return self.sidecars_dir / rel.parent

    def srt_path(self, media: Path, lang: str) -> Path:
        """`foo.mov` → `foo.<lang>.srt`. Default: sibling of the media
        (== transcripts.sidecar_path). NAS: under the sidecars mirror."""
        return self._sidecar_dir_for(media) / f"{media.stem}.{lang}.srt"

    def xmp_path(self, media: Path) -> Path:
        """`foo.MOV` → `foo.xmp`. Default matches sidecar._sidecar_path
        (`media.with_suffix('.xmp')`) byte-for-byte; NAS mirrors."""
        if self.sidecars_dir is None:
            return media.with_suffix(".xmp")
        return self._sidecar_dir_for(media) / f"{media.stem}.xmp"

    def srt_glob(self, media: Path) -> list[Path]:
        """Existing `<stem>.*.srt` sidecars for `media` — searched next to
        the media (default) or in the sidecars mirror (NAS)."""
        d = self._sidecar_dir_for(media)
        if not d.is_dir():
            return []
        return list(d.glob(f"{media.stem}.*.srt"))


def resolve_writable_paths(
    trip_folder: Path,
    *,
    originals_root: Path | None = None,
    state_root: Path | None = None,
    sidecars_root: Path | None = None,
) -> WritablePaths:
    # Local imports: these modules pull heavier deps (video / pg), and
    # keeping them out of paths.py's import-time graph avoids any cycle.
    from .derivatives import DERIVATIVES_DIR
    from .offline import OFFLINE_DIR_NAME

    if originals_root is not None:
        try:
            rel = trip_folder.relative_to(originals_root)
        except ValueError:
            rel = Path(trip_folder.name)
    else:
        rel = Path(trip_folder.name)

    if state_root is not None:
        audit_dir = state_root / rel / AUDIT_DIR
    else:
        audit_dir = trip_folder / AUDIT_DIR

    sidecars_dir = (sidecars_root / rel) if sidecars_root is not None else None

    return WritablePaths(
        audit_dir=audit_dir,
        marker_path=audit_dir / Y_MARKER_FILENAME,
        journal_path=audit_dir / JOURNAL_FILENAME,
        heartbeat_path=audit_dir / HEARTBEAT_FILENAME,
        derivatives_dir=audit_dir / DERIVATIVES_DIR,
        offline_dir=audit_dir / OFFLINE_DIR_NAME,
        trip_folder=trip_folder,
        sidecars_dir=sidecars_dir,
    )


__all__ = ["WritablePaths", "resolve_writable_paths"]
