"""What the browser can show: ffprobe, playability, 480p proxies, thumbnails,
and JPEG previews of raw/HEIC stills. Outputs are written as `._<name>` and
renamed into place, so a failure never leaves a half file behind."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

PLAYABLE_EXTS = frozenset({".mp4", ".mov", ".m4v"})


@dataclass
class Probe:
    duration: float | None
    width: int | None
    height: int | None
    codec: str | None


def ffprobe(path: Path) -> Probe | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        return None
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return None
    streams = j.get("streams") or []
    if not streams:
        return None
    st = streams[0]
    try:
        d = float((j.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        d = None
    return Probe(d, st.get("width"), st.get("height"), st.get("codec_name"))


def playable(codec: str | None, width: int | None, height: int | None, path: Path) -> bool:
    """H.264 up to 1080p (shorter side) in an MP4/MOV plays in every browser."""
    return (codec == "h264" and min(width or 0, height or 0) <= 1080
            and Path(path).suffix.lower() in PLAYABLE_EXTS)


def _staged(out: Path) -> Path:
    return out.with_name("._" + out.name)


def _run_to(out: Path, cmd_with_tmp) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = _staged(out)
    r = subprocess.run(cmd_with_tmp(tmp), capture_output=True)
    if r.returncode or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, out)
    return True


def make_proxy(src: Path, out: Path) -> bool:
    return _run_to(out, lambda tmp: [
        "ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "scale=-2:'min(480,ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-f", "mp4", str(tmp)])


def make_thumb(src: Path, out: Path, at: float = 0.0) -> bool:
    seek = ["-ss", f"{at:.2f}"] if at > 0 else []
    return _run_to(out, lambda tmp: [
        "ffmpeg", "-v", "error", "-y", *seek, "-i", str(src), "-frames:v", "1", "-vf", "scale=320:-2",
        "-q:v", "5", "-f", "image2", str(tmp)])


def make_preview(src: Path, out: Path) -> bool:
    """The camera's embedded JPEG when there is a real one, else an ffmpeg conversion."""
    out.parent.mkdir(parents=True, exist_ok=True)
    for tag in ("-PreviewImage", "-JpgFromRaw"):
        r = subprocess.run(["exiftool", "-b", tag, str(src)], capture_output=True)
        if r.returncode == 0 and len(r.stdout) > 20_000:
            tmp = _staged(out)
            tmp.write_bytes(r.stdout)
            os.replace(tmp, out)
            return True
    return _run_to(out, lambda tmp: [
        "ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", "scale='min(2048,iw)':-2",
        "-f", "image2", str(tmp)])
