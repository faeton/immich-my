"""Phase Y.2 — thumbnail + preview generation via pyvips (libvips).

Immich's Node worker uses Sharp; Sharp wraps libvips, so we get matching
output by calling libvips directly. See docs/IMMICH-INGEST.md §4.2 for the
parameters: 250 px WebP q80 (thumbnail), 1440 px JPEG q80 progressive
(preview).

Output layout mirrors what Immich writes so the push step is a plain
rsync into `<media.host_root>/thumbs/`:

    .audit/derivatives/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_thumbnail.webp
    .audit/derivatives/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_preview.jpeg

After rsync, the `asset_file.path` we INSERT is
`<media.container_root>/thumbs/<userId>/<...>/<id>_*` — same relative
layout under a different root.

Videos (Y.5) are out of scope here; `compute_for_asset` is a no-op for
asset_type='VIDEO'.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyvips


THUMBNAIL_WIDTH = 250
PREVIEW_WIDTH = 1440
QUALITY = 80

DERIVATIVES_DIR = "derivatives"
THUMBS_SUBDIR = "thumbs"

FileKind = Literal["thumbnail", "preview"]


@dataclass(frozen=True)
class DerivativeFile:
    """One derivative output — what we staged and what the DB row should say.

    - `kind` — `asset_file.type` value.
    - `staged_path` — absolute path on the Mac under `.audit/derivatives/`.
      Rsync source.
    - `relative_path` — the `thumbs/<userId>/.../<id>_*` suffix. Stays
      identical on both sides of the rsync; the push step builds the
      container path by prepending `media.container_root`.
    - `is_progressive` / `is_transparent` — go straight into `asset_file`.
    """

    kind: FileKind
    staged_path: Path
    relative_path: str
    is_progressive: bool
    is_transparent: bool


@dataclass(frozen=True)
class DerivativeResult:
    """Per-asset output of `compute_for_asset`: staged files plus the
    source-image dimensions we picked up from libvips.

    Immich's own pipeline writes `asset.width`/`asset.height` from the
    *decoded* image (respecting EXIF orientation), not from the EXIF
    width/height tags — that's what the web viewer uses for layout and
    fullscreen scaling. We surface those here so `process.py` can UPDATE
    the `asset` row in the same transaction that creates the derivatives.
    """

    files: list[DerivativeFile]
    width: int | None
    height: int | None


def _bucket(asset_id: str) -> tuple[str, str]:
    return asset_id[0:2], asset_id[2:4]


def relative_path_for(
    asset_id: str, owner_id: str, kind: FileKind,
) -> str:
    """Return the `thumbs/<userId>/<xx>/<yy>/<id>_<kind>.<ext>` suffix.

    Same string on Mac (under `.audit/derivatives/`) and on NAS (under
    `<media.host_root>/` → container's `<media.container_root>/`).
    """
    ext = "webp" if kind == "thumbnail" else "jpeg"
    a, b = _bucket(asset_id)
    return f"{THUMBS_SUBDIR}/{owner_id}/{a}/{b}/{asset_id}_{kind}.{ext}"


def staged_dir(trip_folder: Path) -> Path:
    from .state import AUDIT_DIR
    return trip_folder / AUDIT_DIR / DERIVATIVES_DIR


def _write_thumbnail(src: Path, dst: Path) -> None:
    """250 px WebP, quality 80 — Immich's `thumbnail.webp` spec."""
    image = pyvips.Image.thumbnail(str(src), THUMBNAIL_WIDTH)
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.webpsave(str(dst), Q=QUALITY, strip=True)


def _write_preview(src: Path, dst: Path) -> None:
    """1440 px JPEG, quality 80, progressive — Immich's `preview.jpeg` spec."""
    image = pyvips.Image.thumbnail(str(src), PREVIEW_WIDTH)
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.jpegsave(str(dst), Q=QUALITY, interlace=True, strip=True)


def compute_for_asset(
    *,
    source_media: Path,
    asset_id: str,
    owner_id: str,
    asset_type: str,
    trip_folder: Path,
) -> DerivativeResult:
    """Write thumbnail + preview for one IMAGE asset.

    Returns staged derivatives plus decoded dimensions (width/height
    after EXIF-orientation auto-rotation — matching what Sharp reports
    to Immich). For VIDEO we return an empty result (Y.5 handles video
    proxies separately). Raises `pyvips.Error` on decode failure —
    caller decides whether to skip or abort.
    """
    if asset_type != "IMAGE":
        return DerivativeResult(files=[], width=None, height=None)

    base = staged_dir(trip_folder)
    files: list[DerivativeFile] = []

    # Read original dimensions once, autorotating per EXIF:Orientation so
    # the reported w/h matches what Immich's viewer expects after the
    # preview gets rendered. libvips is lazy — autorot only rewrites
    # metadata, not pixels, until a save forces evaluation, so this is
    # cheap even for huge originals.
    src_img = pyvips.Image.new_from_file(str(source_media), access="sequential")
    src_img = src_img.autorot()
    width = int(src_img.width)
    height = int(src_img.height)

    for kind in ("thumbnail", "preview"):
        rel = relative_path_for(asset_id, owner_id, kind)
        dst = base / rel
        if kind == "thumbnail":
            _write_thumbnail(source_media, dst)
        else:
            _write_preview(source_media, dst)
        files.append(DerivativeFile(
            kind=kind,
            staged_path=dst,
            relative_path=rel,
            is_progressive=(kind == "preview"),
            is_transparent=False,
        ))
    return DerivativeResult(files=files, width=width, height=height)


__all__ = [
    "DerivativeFile", "DerivativeResult",
    "THUMBNAIL_WIDTH", "PREVIEW_WIDTH", "QUALITY",
    "DERIVATIVES_DIR", "THUMBS_SUBDIR",
    "compute_for_asset", "relative_path_for", "staged_dir",
]
