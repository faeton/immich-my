"""Phase Y.2 / Y.5 — thumbnail + preview generation via pyvips (libvips),
plus video poster + optional transcode via ffmpeg.

Immich's Node worker uses Sharp; Sharp wraps libvips, so we get matching
output for stills by calling libvips directly. See docs/IMMICH-INGEST.md
§4.2 / §4.5 for the parameters: 250 px WebP q80 (thumbnail), 1440 px
JPEG q80 progressive (preview). For videos we extract a single-frame
poster via ffmpeg and feed that through the exact same pyvips resize.

Output layout mirrors what Immich writes so the push step is a plain
rsync into `<media.host_root>/`:

    .audit/derivatives/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_thumbnail.webp
    .audit/derivatives/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_preview.jpeg
    .audit/derivatives/encoded-video/<userId>/<id[0:2]>/<id[2:4]>/<id>.mp4

After rsync, the `asset_file.path` we INSERT is
`<media.container_root>/thumbs/<userId>/<...>/<id>_*` (same relative
layout under a different root). The transcoded mp4 lands at
`<media.container_root>/encoded-video/<userId>/<...>/<id>.mp4`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    import pyvips
except (ImportError, OSError) as e:
    pyvips = None
    _PYVIPS_IMPORT_ERROR = e
else:
    _PYVIPS_IMPORT_ERROR = None

from . import video as video_mod


THUMBNAIL_WIDTH = 250
PREVIEW_WIDTH = 1440
QUALITY = 80

DERIVATIVES_DIR = "derivatives"
THUMBS_SUBDIR = "thumbs"
ENCODED_VIDEO_SUBDIR = "encoded-video"

# RAW formats where `new_from_file` triggers a full libraw demosaic — skip
# the dim-probe and rely on EXIF-supplied dims instead. `thumbnail()` itself
# uses libraw's embedded preview, which is orders of magnitude cheaper.
_RAW_SUFFIXES = {".dng", ".nef", ".arw", ".cr2", ".cr3", ".rw2", ".raf", ".orf", ".srw"}

FileKind = Literal["thumbnail", "preview", "encoded_video"]


def _require_pyvips():
    if pyvips is None:
        detail = f": {_PYVIPS_IMPORT_ERROR}" if _PYVIPS_IMPORT_ERROR else ""
        raise RuntimeError(
            "pyvips/libvips is unavailable; derivative generation requires "
            "`brew install vips` on macOS"
            f"{detail}"
        )
    return pyvips


def _save_kwargs(vips_module) -> dict[str, object]:
    """Metadata-stripping saver args across libvips versions.

    libvips 8.15 deprecated `strip`; the replacement is `keep="none"`.
    Keep the old flag for older libvips so the code still runs on machines
    that haven't picked up the newer save API yet.
    """
    if hasattr(vips_module, "at_least_libvips") and vips_module.at_least_libvips(8, 15):
        return {"keep": "none"}
    return {"strip": True}


@dataclass(frozen=True)
class DerivativeFile:
    """One derivative output — what we staged and what the DB row should say.

    - `kind` — `asset_file.type` value (`thumbnail` | `preview` |
      `encoded_video`).
    - `staged_path` — absolute path on the Mac under `.audit/derivatives/`.
      Rsync source.
    - `relative_path` — the `thumbs/<userId>/.../<id>_*` or
      `encoded-video/<userId>/.../<id>.mp4` suffix. Stays identical on
      both sides of the rsync; the push step builds the container path
      by prepending `media.container_root`.
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
    source-image dimensions we picked up from libvips/ffprobe.

    Immich's own pipeline writes `asset.width`/`asset.height` from the
    *decoded* image (respecting EXIF orientation), not from the EXIF
    width/height tags — that's what the web viewer uses for layout and
    fullscreen scaling. For videos we use ffprobe + rotation side-data
    so portrait iPhone clips report portrait dims.

    `duration` is non-None only for videos; surfaced so `process.py`
    can overwrite an EXIF-derived `asset.duration` with the ffprobe
    value when they disagree (ffprobe wins — it reads the container
    directly instead of guessing from QuickTime tags).
    """

    files: list[DerivativeFile]
    width: int | None
    height: int | None
    duration: str | None = None


def _bucket(asset_id: str) -> tuple[str, str]:
    return asset_id[0:2], asset_id[2:4]


def relative_path_for(
    asset_id: str, owner_id: str, kind: FileKind,
) -> str:
    """Return the staged-relative path for a given derivative kind.

    - `thumbnail`/`preview` → `thumbs/<userId>/<xx>/<yy>/<id>_<kind>.<ext>`
    - `encoded_video` → `encoded-video/<userId>/<xx>/<yy>/<id>.mp4`

    Same string on Mac (under `.audit/derivatives/`) and on NAS (under
    `<media.host_root>/` → container's `<media.container_root>/`).
    """
    a, b = _bucket(asset_id)
    if kind == "encoded_video":
        return f"{ENCODED_VIDEO_SUBDIR}/{owner_id}/{a}/{b}/{asset_id}.mp4"
    ext = "webp" if kind == "thumbnail" else "jpeg"
    return f"{THUMBS_SUBDIR}/{owner_id}/{a}/{b}/{asset_id}_{kind}.{ext}"


def staged_dir(trip_folder: Path) -> Path:
    from .state import AUDIT_DIR
    return trip_folder / AUDIT_DIR / DERIVATIVES_DIR


def _save_preview(image, dst: Path) -> None:
    vips = _require_pyvips()
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.jpegsave(str(dst), Q=QUALITY, interlace=True, **_save_kwargs(vips))


def _save_thumbnail(image, dst: Path) -> None:
    vips = _require_pyvips()
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.webpsave(str(dst), Q=QUALITY, **_save_kwargs(vips))


def _image_dims_and_stills(
    source_media: Path, asset_id: str, owner_id: str, base: Path,
) -> tuple[list[DerivativeFile], int | None, int | None]:
    """IMAGE branch: decode once via libvips thumbnail, emit two stills.

    We used to call `vips.Image.thumbnail(src, …)` twice (once per size)
    *and* `new_from_file` for dims — three decodes per asset. For RAW
    files (.dng etc.) each decode goes through libraw, so the same image
    was demosaiced three times. Now: one `thumbnail(src, PREVIEW_WIDTH)`
    call (which uses shrink-on-load for JPEG and libraw's embedded
    preview for RAW), then `thumbnail_image` in-memory for the 250 px
    WebP. Dim probe is skipped for RAW — EXIF already gave us dims, and
    reprobing via `new_from_file` would re-trigger the full demosaic.
    """
    vips = _require_pyvips()
    # `thumbnail()` returns a pipeline bound to a sequential loader, so
    # consuming it twice (preview save + derive-and-save thumbnail) would
    # trip libvips' "out of order read" guard. `copy_memory()` materialises
    # the 1440 px image once so both saves read from RAM.
    preview_img = vips.Image.thumbnail(
        str(source_media), PREVIEW_WIDTH,
    ).copy_memory()

    width: int | None = None
    height: int | None = None
    if source_media.suffix.lower() not in _RAW_SUFFIXES:
        try:
            hdr = vips.Image.new_from_file(
                str(source_media), access="sequential",
            ).autorot()
            width = int(hdr.width)
            height = int(hdr.height)
        except Exception:
            width = height = None

    preview_rel = relative_path_for(asset_id, owner_id, "preview")
    preview_dst = base / preview_rel
    _save_preview(preview_img, preview_dst)

    thumb_rel = relative_path_for(asset_id, owner_id, "thumbnail")
    thumb_dst = base / thumb_rel
    _save_thumbnail(
        vips.Image.thumbnail_image(preview_img, THUMBNAIL_WIDTH),
        thumb_dst,
    )

    files = [
        DerivativeFile(
            kind="thumbnail", staged_path=thumb_dst, relative_path=thumb_rel,
            is_progressive=False, is_transparent=False,
        ),
        DerivativeFile(
            kind="preview", staged_path=preview_dst, relative_path=preview_rel,
            is_progressive=True, is_transparent=False,
        ),
    ]
    return files, width, height


def _video_stills_and_transcode(
    source_media: Path, asset_id: str, owner_id: str, base: Path,
    *, transcode: bool,
) -> tuple[list[DerivativeFile], int, int, str | None]:
    """VIDEO branch: ffprobe → poster → two stills via pyvips (+ optional
    transcode). Returns (files, width, height, duration).

    The poster is a temp JPEG inside `.audit/derivatives/` that we feed
    to the same `_write_thumbnail` / `_write_preview` helpers used for
    still assets — keeps the tile appearance identical regardless of
    asset type. Poster itself is left on disk (cheap, aids debug) but
    we don't insert an `asset_file` row for it.
    """
    info = video_mod.probe(source_media)
    duration_str = (
        video_mod.format_duration(info.duration_s)
        if info.duration_s is not None else None
    )

    poster = base / "_posters" / f"{asset_id}.jpg"
    video_mod.extract_poster(source_media, poster, duration_s=info.duration_s)

    vips = _require_pyvips()
    preview_img = vips.Image.thumbnail(
        str(poster), PREVIEW_WIDTH,
    ).copy_memory()

    preview_rel = relative_path_for(asset_id, owner_id, "preview")
    preview_dst = base / preview_rel
    _save_preview(preview_img, preview_dst)

    thumb_rel = relative_path_for(asset_id, owner_id, "thumbnail")
    thumb_dst = base / thumb_rel
    _save_thumbnail(
        vips.Image.thumbnail_image(preview_img, THUMBNAIL_WIDTH),
        thumb_dst,
    )

    files: list[DerivativeFile] = [
        DerivativeFile(
            kind="thumbnail", staged_path=thumb_dst, relative_path=thumb_rel,
            is_progressive=False, is_transparent=False,
        ),
        DerivativeFile(
            kind="preview", staged_path=preview_dst, relative_path=preview_rel,
            is_progressive=True, is_transparent=False,
        ),
    ]

    if transcode and video_mod.needs_transcode(info):
        rel = relative_path_for(asset_id, owner_id, "encoded_video")
        dst = base / rel
        video_mod.transcode(source_media, dst)
        files.append(DerivativeFile(
            kind="encoded_video",
            staged_path=dst,
            relative_path=rel,
            is_progressive=False,
            is_transparent=False,
        ))

    return files, info.width, info.height, duration_str


def compute_for_asset(
    *,
    source_media: Path,
    asset_id: str,
    owner_id: str,
    asset_type: str,
    trip_folder: Path,
    transcode_videos: bool = True,
) -> DerivativeResult:
    """Stage thumbnail + preview (and for videos, optional encoded_video)
    for one asset.

    Returns staged derivatives plus decoded dimensions (rotated per EXIF
    orientation for stills, per ffprobe `side_data_list[].rotation` for
    videos — matching what Immich's own pipeline reports). `duration`
    is only set for videos. Raises on decode/probe/transcode failure —
    caller decides whether to skip or abort.
    """
    base = staged_dir(trip_folder)

    if asset_type == "IMAGE":
        files, w_opt, h_opt = _image_dims_and_stills(
            source_media, asset_id, owner_id, base,
        )
        return DerivativeResult(
            files=files, width=w_opt, height=h_opt, duration=None,
        )

    if asset_type == "VIDEO":
        files, w, h, dur = _video_stills_and_transcode(
            source_media, asset_id, owner_id, base,
            transcode=transcode_videos,
        )
        return DerivativeResult(files=files, width=w, height=h, duration=dur)

    return DerivativeResult(files=[], width=None, height=None, duration=None)


__all__ = [
    "DerivativeFile", "DerivativeResult",
    "THUMBNAIL_WIDTH", "PREVIEW_WIDTH", "QUALITY",
    "DERIVATIVES_DIR", "THUMBS_SUBDIR", "ENCODED_VIDEO_SUBDIR",
    "compute_for_asset", "relative_path_for", "staged_dir",
]
