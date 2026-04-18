"""Flag videos encoded well above delivery-quality bitrate.

This is the Phase 2a half of the Phase 2c pipeline: just *identify*
files that Phase 2c's group-by-folder confirm UI should offer to
transcode. No rewriting happens here — this is a LOW `note` action
so the file surfaces in the per-file flags column.

Score = `bitrate / (w * h * fps)` (bits-per-pixel-per-frame). Thresholds
from PLAN.md, tuned conservatively so edit sources never trip the rule:

- H.264 delivery: sane <0.15, fat 0.15–0.30, obscene >0.30
- HEVC delivery: sane <0.08, fat 0.08–0.15, obscene >0.15

**Preserve allowlist — rule stands down, no flag.** These are edit
sources or already-lean deliveries:

- Extensions: `.insv`, `.insp`, `.lrv`, `.lrf`, `.mts`, `.dng`, `.braw`,
  `.prores`, `.mov` with ProRes/DNxHD codec (see codec check).
- Filename prefixes (camera-native, never flag even at high bitrate):
  `DJI_`, `GX`, `GH`, `GOPR`, `MAH`, `MVI_`, `C0`, `LRV_`, `PRO_`,
  date-stamped `VID_YYYYMMDD`, `IMG_YYYYMMDD`, `DSC_`.
- Codec: prores, dnxhd/dnxhr, cineform, ffv1, raw.
- **All Insta360 content**, including exported 5.7K/7.7K `.mp4` at
  equirectangular 2:1 aspect — those are re-edit sources, not
  deliveries (confirmed user preference).
- Folder segment contains `raw`, `source`, `edit`, `project`.

The rule is intentionally quiet. Most trips produce zero flags;
Phase 2c's confirm UI does the actual gatekeeping when flags appear.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..exif import ExifRow
from .registry import Finding, Rule, register


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts"}

PRESERVE_EXTS = {".insv", ".insp", ".lrv", ".lrf", ".mts", ".dng", ".braw"}

PRESERVE_PREFIXES = ("DJI_", "GX", "GH", "GOPR", "MAH", "MVI_", "C0",
                     "LRV_", "PRO_", "DSC_")

PRESERVE_CODECS = ("prores", "dnxh", "cineform", "ffv1", "raw", "cine",
                   "apch", "apcn", "apcs", "apco", "ap4h", "ap4x")

PRESERVE_FOLDER_SEGMENTS = ("raw", "source", "edit", "project", "insta360")

H264_CODECS = ("avc1", "h264", "x264")
HEVC_CODECS = ("hvc1", "hev1", "hevc", "h265", "x265")

H264_FAT_BPP = 0.15
HEVC_FAT_BPP = 0.08

_DATE_PREFIX_RE = re.compile(r"^(VID|IMG)_\d{8}")


def _is_insta360(path: Path, raw: dict) -> bool:
    if path.suffix.lower() in (".insv", ".insp", ".lrv"):
        return True
    for k in ("QuickTime:Make", "EXIF:Make", "XMP:Make", "QuickTime:HandlerDescription"):
        v = raw.get(k)
        if isinstance(v, str) and "insta360" in v.lower():
            return True
    return False


def _preserve_by_name(path: Path) -> bool:
    name = path.name
    stem_upper = name.upper()
    if any(stem_upper.startswith(p) for p in PRESERVE_PREFIXES):
        return True
    if _DATE_PREFIX_RE.match(name):
        return True
    return False


def _preserve_by_folder(path: Path) -> bool:
    # Substring match inside any DIRECTORY segment — mirrors PLAN.md's
    # `*raw*` / `*source*` / `*edit*` / `*project*` glob intent. The
    # filename itself is excluded; we don't want `edit.mp4` to escape
    # flagging just because its name happens to contain "edit".
    for part in path.parent.parts:
        lower = part.lower()
        if any(seg in lower for seg in PRESERVE_FOLDER_SEGMENTS):
            return True
    return False


def _codec(raw: dict) -> str:
    for k in ("QuickTime:CompressorID", "QuickTime:VideoCodec",
              "QuickTime:CompressorName"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def _num(raw: dict, *keys: str) -> float | None:
    for k in keys:
        v = raw.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _score(row: ExifRow) -> tuple[float, float, str] | None:
    """Return (bpp, threshold, codec_family) or None if unscorable."""
    codec = _codec(row.raw)
    if any(tag in codec for tag in PRESERVE_CODECS):
        return None
    if any(tag in codec for tag in HEVC_CODECS):
        family, threshold = "hevc", HEVC_FAT_BPP
    elif any(tag in codec for tag in H264_CODECS):
        family, threshold = "h264", H264_FAT_BPP
    else:
        return None

    w = _num(row.raw, "QuickTime:ImageWidth", "Composite:ImageWidth", "EXIF:ExifImageWidth")
    h = _num(row.raw, "QuickTime:ImageHeight", "Composite:ImageHeight", "EXIF:ExifImageHeight")
    fps = _num(row.raw, "QuickTime:VideoFrameRate", "Composite:VideoFrameRate")
    bitrate = _num(row.raw, "Composite:AvgBitrate", "QuickTime:AvgBitrate")
    if bitrate is None:
        size = _num(row.raw, "File:FileSize")
        dur = _num(row.raw, "QuickTime:Duration", "Composite:Duration")
        if size is not None and dur and dur > 0:
            bitrate = size * 8 / dur
    if not (w and h and fps and bitrate):
        return None

    bpp = bitrate / (w * h * fps)
    return (bpp, threshold, family)


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        if row.path.suffix.lower() not in VIDEO_EXTS:
            continue
        if row.path.suffix.lower() in PRESERVE_EXTS:
            continue
        if _preserve_by_name(row.path):
            continue
        if _preserve_by_folder(row.path):
            continue
        if _is_insta360(row.path, row.raw):
            continue
        scored = _score(row)
        if scored is None:
            continue
        bpp, threshold, family = scored
        if bpp < threshold:
            continue
        tier = "fat" if bpp < threshold * 2 else "obscene"
        reason = (
            f"{family} @ {bpp:.3f} bpp ({tier}; delivery sane <{threshold:.2f}) — "
            f"candidate for Phase 2c transcode"
        )
        out.append(Finding(
            rule="bloat-candidate",
            confidence="low",
            path=row.path,
            action="note",
            reason=reason,
        ))
    return out


register(Rule(name="bloat-candidate", confidence="low", propose=_propose))
