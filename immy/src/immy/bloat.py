"""Phase 2c — bloat detector + HEVC re-encode workflow.

CLI-first. Detection reuses the `bloat-candidate` rule's allowlist and
bits-per-pixel-per-frame score; this module adds the bits the rule
deliberately skipped:

- Walk a tree, collect candidates into `BloatCandidate` records with
  current size + estimated post-transcode size.
- Group by parent folder (per user preference: confirm per folder, never
  auto — see feedback_transcode_confirm).
- Target bitrate table keyed by `(w * h * fps)` for HEVC delivery.
- `ffmpeg -c:v hevc_videotoolbox -tag:v hvc1` transcode with duration +
  stream-count verification.
- Non-destructive by default: output lands at `<stem>.optimized.<ext>`
  next to the source. `--apply` atomic-renames original to
  `<name>.original`, optimized to the source path, and writes a
  `.transcode.json` receipt with the pre-sha256 + original size + codec.

Never runs ffmpeg on import; scan is pure metadata. Keeps this module
cheap to pull into tests and into the `immy audit` preview.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .exif import ExifRow, read_folder
from .rules.bloat_candidate import (
    HEVC_FAT_BPP,
    PRESERVE_EXTS,
    VIDEO_EXTS,
    _codec,
    _is_insta360,
    _num,
    _preserve_by_folder,
    _preserve_by_name,
    _score,
)


# HEVC delivery bits-per-pixel-per-frame target. Sane-end of the rule's
# threshold (0.08) would produce files that still flag themselves — aim
# below that so a re-run is clean.
HEVC_TARGET_BPP = 0.05

# Round target bitrate to nearest N kbps so the table is friendly to read.
_BITRATE_ROUND_KBPS = 500_000

# Never transcode if the savings fall below this fraction of the source
# size. CPU + quality hit not worth the gain.
MIN_SAVINGS_FRACTION = 0.20


@dataclass
class BloatCandidate:
    path: Path
    width: int
    height: int
    fps: float
    current_bitrate: int      # bits per second
    current_size: int         # bytes
    codec_family: str         # "h264" | "hevc"
    tier: str                 # "fat" | "obscene"
    target_bitrate: int       # bits per second
    estimated_size: int       # bytes after transcode

    @property
    def savings_bytes(self) -> int:
        return max(0, self.current_size - self.estimated_size)

    @property
    def savings_fraction(self) -> float:
        if self.current_size <= 0:
            return 0.0
        return self.savings_bytes / self.current_size


def target_bitrate(w: int, h: int, fps: float) -> int:
    """HEVC delivery target bitrate in bits/sec, rounded to nearest 0.5 Mbps."""
    raw = w * h * fps * HEVC_TARGET_BPP
    return int(round(raw / _BITRATE_ROUND_KBPS) * _BITRATE_ROUND_KBPS) or _BITRATE_ROUND_KBPS


def _eligible(row: ExifRow) -> bool:
    """Same gate the rule uses — preserve allowlist first, codec/metric last."""
    path = row.path
    if path.suffix.lower() not in VIDEO_EXTS:
        return False
    if path.suffix.lower() in PRESERVE_EXTS:
        return False
    if _preserve_by_name(path):
        return False
    if _preserve_by_folder(path):
        return False
    if _is_insta360(path, row.raw):
        return False
    return True


def _candidate_from_row(row: ExifRow) -> BloatCandidate | None:
    if not _eligible(row):
        return None
    scored = _score(row)
    if scored is None:
        return None
    bpp, threshold, family = scored
    if bpp < threshold:
        return None
    tier = "fat" if bpp < threshold * 2 else "obscene"

    w = int(_num(row.raw, "QuickTime:ImageWidth", "Composite:ImageWidth") or 0)
    h = int(_num(row.raw, "QuickTime:ImageHeight", "Composite:ImageHeight") or 0)
    fps = float(_num(row.raw, "QuickTime:VideoFrameRate", "Composite:VideoFrameRate") or 0)
    bitrate = _num(row.raw, "Composite:AvgBitrate", "QuickTime:AvgBitrate")
    size = _num(row.raw, "File:FileSize")
    dur = _num(row.raw, "QuickTime:Duration", "Composite:Duration")
    if bitrate is None and size and dur and dur > 0:
        bitrate = size * 8 / dur
    if size is None and bitrate and dur and dur > 0:
        size = bitrate * dur / 8
    if not (w and h and fps and bitrate and size):
        return None

    tb = target_bitrate(w, h, fps)
    estimated = int(tb * (size / max(bitrate, 1)))

    # Skip if projected savings too small to be worth the CPU + quality hit.
    if size <= 0 or (size - estimated) / size < MIN_SAVINGS_FRACTION:
        return None

    return BloatCandidate(
        path=row.path,
        width=w,
        height=h,
        fps=fps,
        current_bitrate=int(bitrate),
        current_size=int(size),
        codec_family=family,
        tier=tier,
        target_bitrate=tb,
        estimated_size=estimated,
    )


def scan(folder: Path) -> list[BloatCandidate]:
    """Walk `folder`, return bloat candidates (detection only, no writes)."""
    rows = read_folder(folder)
    out: list[BloatCandidate] = []
    for row in rows:
        cand = _candidate_from_row(row)
        if cand is not None:
            out.append(cand)
    return out


def group_by_folder(
    candidates: Iterable[BloatCandidate], root: Path
) -> dict[Path, list[BloatCandidate]]:
    """Group by immediate parent directory. Preserves encounter order."""
    groups: dict[Path, list[BloatCandidate]] = {}
    for c in candidates:
        parent = c.path.parent
        try:
            parent = parent.relative_to(root)
        except ValueError:
            pass
        groups.setdefault(parent, []).append(c)
    return groups


# --- transcode -------------------------------------------------------------


class TranscodeError(RuntimeError):
    pass


def optimized_path(src: Path) -> Path:
    return src.with_name(f"{src.stem}.optimized{src.suffix}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffprobe_streams(path: Path) -> tuple[float, int]:
    """Return (duration_sec, stream_count). Raises TranscodeError on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=index",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise TranscodeError(f"ffprobe failed on {path}: {e}") from e
    data = json.loads(out.stdout)
    dur = float(data.get("format", {}).get("duration") or 0)
    streams = len(data.get("streams") or [])
    return dur, streams


def _verify(src: Path, dst: Path, tolerance: float = 0.5) -> None:
    """Duration ±0.5 s and stream count match. Raises on mismatch."""
    src_dur, src_streams = _ffprobe_streams(src)
    dst_dur, dst_streams = _ffprobe_streams(dst)
    if abs(src_dur - dst_dur) > tolerance:
        raise TranscodeError(
            f"duration mismatch on {dst.name}: "
            f"src={src_dur:.2f}s dst={dst_dur:.2f}s (tol {tolerance}s)"
        )
    if src_streams != dst_streams:
        raise TranscodeError(
            f"stream count mismatch on {dst.name}: "
            f"src={src_streams} dst={dst_streams}"
        )


def transcode_one(
    c: BloatCandidate,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
) -> Path:
    """Run hevc_videotoolbox on `c.path`, leave output at `.optimized.ext`.

    Non-destructive. Verifies duration + stream count on completion.
    Idempotent: returns existing optimized file untouched unless
    `overwrite=True`. `dry_run` prints the plan and returns the target path.
    """
    dst = optimized_path(c.path)
    if dry_run:
        return dst
    if dst.exists() and not overwrite:
        return dst

    if shutil.which("ffmpeg") is None:
        raise TranscodeError("ffmpeg not on PATH")

    tmp = dst.with_suffix(dst.suffix + ".part")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(c.path),
        "-c:v", "hevc_videotoolbox", "-tag:v", "hvc1",
        "-b:v", str(c.target_bitrate),
        "-c:a", "copy",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        if tmp.exists():
            tmp.unlink()
        raise TranscodeError(f"ffmpeg failed on {c.path.name}: rc={e.returncode}") from e

    _verify(c.path, tmp)
    tmp.rename(dst)
    return dst


def apply_one(c: BloatCandidate, optimized: Path) -> Path:
    """Atomic-replace: rename original to `<name>.original`, optimized to
    the source path. Writes a JSON receipt next to the new file with the
    pre-transcode sha256, size, codec, and bitrate.

    Returns the receipt path.
    """
    if not optimized.exists():
        raise TranscodeError(f"optimized file missing: {optimized}")

    pre_sha = _sha256(c.path)
    backup = c.path.with_name(c.path.name + ".original")
    receipt = c.path.with_name(c.path.name + ".transcode.json")

    c.path.rename(backup)
    optimized.rename(c.path)

    post_size = c.path.stat().st_size
    data = {
        "pre_sha256": pre_sha,
        "pre_size": c.current_size,
        "post_size": post_size,
        "pre_bitrate": c.current_bitrate,
        "post_bitrate_target": c.target_bitrate,
        "codec_before": c.codec_family,
        "codec_after": "hevc",
        "width": c.width,
        "height": c.height,
        "fps": c.fps,
        "original_name": backup.name,
    }
    receipt.write_text(json.dumps(data, indent=2))
    return receipt


# --- formatting helpers used by CLI output --------------------------------


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def fmt_bitrate(bps: int) -> str:
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps / 1_000:.0f} kbps"


def candidate_dict(c: BloatCandidate, root: Path) -> dict:
    """For JSON output / debugging."""
    d = asdict(c)
    d["path"] = str(c.path.relative_to(root)) if root in c.path.parents else str(c.path)
    return d
