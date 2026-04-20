"""Phase Y.5 — video probe + poster-frame extraction + optional transcode.

Mirrors what Immich's media pipeline does for videos:

- Duration + display dimensions come from `ffprobe` (see
  docs/IMMICH-INGEST.md §4.2 / §4.5). Rotation from `side_data_list`
  is applied so `asset.width`/`asset.height` match what the viewer
  actually shows (portrait iPhone clips report 1920x1080 with a −90°
  rotation side-tag; Immich's Sharp pipeline ends up with 1080x1920).

- Poster JPEG is pulled at `min(duration/2, 5 s)` via a single
  `ffmpeg -ss T -i ... -frames:v 1` call. libvips then downscales
  the poster into the standard 250 px WebP thumbnail + 1440 px JPEG
  preview — same output spec as `derivatives._write_thumbnail` /
  `_write_preview` for stills, so the UI gets identical-looking tiles
  regardless of asset type.

- Transcode is optional. `needs_transcode` implements a conservative
  match for Immich's default "Required" TranscodePolicy: if the source
  is already h264 + aac (or no audio) in an mp4/mov container AND not
  taller than 720 px, the source itself is web-playable and we skip.
  Otherwise we produce `<id>.mp4` with `libx264 -crf 23 -preset
  ultrafast -c:a aac -movflags +faststart -vf scale=-2:'min(720,ih)'`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


POSTER_SEEK_CAP_SEC = 5.0  # don't seek deeper than this for the poster
TRANSCODE_TARGET_HEIGHT = 720
# Container extensions we consider browser-safe when paired with a
# browser-safe codec. Everything else (.mkv, .avi, .mts, .insv, .lrv…)
# triggers a transcode regardless of codec.
_WEB_CONTAINERS = {".mp4", ".mov", ".m4v"}
_WEB_VIDEO_CODECS = {"h264"}
_WEB_AUDIO_CODECS = {"aac", "mp3", ""}  # "" → no audio


class VideoProbeError(RuntimeError):
    """ffprobe failed or returned something we couldn't parse."""


class VideoTranscodeError(RuntimeError):
    """ffmpeg failed during poster extraction or transcode."""


@dataclass(frozen=True)
class VideoInfo:
    """Subset of `ffprobe` output we care about.

    `width`/`height` are *display* dimensions — coded dims rotated by
    `rotation` when a ±90° side-data tag is present. Everything else
    is verbatim from ffprobe.
    """

    width: int
    height: int
    duration_s: float | None
    video_codec: str
    audio_codec: str  # "" when no audio stream
    container_ext: str  # lowercased suffix incl. dot, e.g. ".mp4"


def _ffprobe_json(path: Path) -> dict:
    if shutil.which("ffprobe") is None:
        raise VideoProbeError("ffprobe not on PATH — install ffmpeg")
    args = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise VideoProbeError(
            f"ffprobe exit {e.returncode} on {path.name}: "
            f"{(e.stderr or '').strip()[:200]}"
        ) from e
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as e:
        raise VideoProbeError(f"ffprobe output not JSON: {e}") from e


def _rotation_from_stream(stream: dict) -> int:
    """Return rotation in degrees, normalised to {0, 90, 180, 270}.

    Modern ffprobe exposes rotation via `side_data_list[].rotation`
    (signed int, negative = clockwise). Older files have a literal
    `tags.rotate` string. Both forms show up in real trips, so we
    try both — iPhone portrait clips lean on side_data_list.
    """
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(sd["rotation"]) % 360
            except (TypeError, ValueError):
                continue
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            return int(tags["rotate"]) % 360
        except (TypeError, ValueError):
            pass
    return 0


def probe(path: Path) -> VideoInfo:
    """Read container + stream metadata via `ffprobe`.

    Raises `VideoProbeError` on any failure — caller decides whether
    to skip the asset or abort the whole trip. Duration falls back to
    `format.duration` when the video stream doesn't declare one (e.g.
    some MTS captures)."""
    data = _ffprobe_json(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise VideoProbeError(f"{path.name}: no video stream")

    raw_w = int(video.get("width") or 0)
    raw_h = int(video.get("height") or 0)
    rot = _rotation_from_stream(video)
    if rot in (90, 270):
        width, height = raw_h, raw_w
    else:
        width, height = raw_w, raw_h

    # Duration: prefer format-level (container-accurate), fall back to
    # stream (some trimming tools drop format.duration).
    dur_raw = (data.get("format") or {}).get("duration") or video.get("duration")
    try:
        duration_s = float(dur_raw) if dur_raw is not None else None
    except (TypeError, ValueError):
        duration_s = None

    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    audio_codec = str(audio.get("codec_name") or "") if audio else ""

    return VideoInfo(
        width=width,
        height=height,
        duration_s=duration_s,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=audio_codec,
        container_ext=path.suffix.lower(),
    )


def format_duration(seconds: float) -> str:
    """Render ffprobe seconds as Immich's `HH:MM:SS.sss` string — the
    format `asset.duration` expects. We zero-pad all three fields so
    ORDER BY on the text column sorts correctly."""
    if seconds < 0:
        seconds = 0.0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def needs_transcode(info: VideoInfo) -> bool:
    """Match Immich's default `TranscodePolicy=Required` behaviour.

    Skip transcode only when every axis of the source is already web-
    playable: H.264 video, AAC-or-silent audio, mp4/mov container, and
    not taller than our 720 px preview target. Anything else gets a
    fresh h264/aac mp4 so the browser's `<video>` element can play it
    without fetch+demux weirdness."""
    if info.video_codec not in _WEB_VIDEO_CODECS:
        return True
    if info.audio_codec not in _WEB_AUDIO_CODECS:
        return True
    if info.container_ext not in _WEB_CONTAINERS:
        return True
    if info.height > TRANSCODE_TARGET_HEIGHT:
        return True
    return False


def extract_poster(src: Path, dst: Path, *, duration_s: float | None) -> None:
    """Write a single-frame JPEG poster.

    Seeks to `min(duration/2, 5 s)`; for broken/zero-duration files
    we just seek to 0. `-ss` is placed *before* `-i` so ffmpeg does
    the fast (keyframe-level) seek — accurate-seek would re-decode
    from frame 0 on every call and crawl on long takes.
    """
    if shutil.which("ffmpeg") is None:
        raise VideoTranscodeError("ffmpeg not on PATH — install ffmpeg")
    seek = 0.0
    if duration_s is not None and duration_s > 0:
        seek = min(duration_s / 2.0, POSTER_SEEK_CAP_SEC)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seek:.3f}",
        "-i", str(src),
        "-frames:v", "1",
        "-q:v", "2",  # high-quality JPEG; source for later pyvips resize
        "-y", str(dst),
    ]
    try:
        subprocess.run(args, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise VideoTranscodeError(
            f"poster extract failed on {src.name}: "
            f"{(e.stderr or '').strip()[:200]}"
        ) from e


def transcode(src: Path, dst: Path) -> None:
    """Produce a web-playable mp4 at the target height.

    Encoder choices mirror Immich's defaults (see `IMMICH-INGEST.md`
    §4.5): libx264 CRF 23 preset ultrafast, AAC 128 kbps, faststart
    so the browser can start playback before the full file arrives.
    Scale clamps the shorter axis to `TRANSCODE_TARGET_HEIGHT` only
    when the source is taller — never upscales.
    """
    if shutil.which("ffmpeg") is None:
        raise VideoTranscodeError("ffmpeg not on PATH — install ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale='trunc(iw*min(1,{TRANSCODE_TARGET_HEIGHT}/ih)/2)*2':'min({TRANSCODE_TARGET_HEIGHT},ih)'"
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-vf", vf,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-y", str(dst),
    ]
    try:
        subprocess.run(args, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise VideoTranscodeError(
            f"transcode failed on {src.name}: "
            f"{(e.stderr or '').strip()[:200]}"
        ) from e


__all__ = [
    "VideoInfo", "VideoProbeError", "VideoTranscodeError",
    "probe", "format_duration", "needs_transcode",
    "extract_poster", "transcode",
    "POSTER_SEEK_CAP_SEC", "TRANSCODE_TARGET_HEIGHT",
]
