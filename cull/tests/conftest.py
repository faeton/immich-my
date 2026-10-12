from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
HAVE_EXIFTOOL = shutil.which("exiftool") is not None


def make_clip(path: Path, *, seconds: float = 2.0, size: str = "320x240", codec: str = "libx264",
              utc: str | None = "2022-01-30T10:29:38") -> Path:
    """A tiny clip; `codec="mpeg4"` makes one the browser cannot play (needs a proxy)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration={seconds}",
           "-c:v", codec, "-pix_fmt", "yuv420p", "-g", "5"]
    if utc:
        cmd += ["-metadata", f"creation_time={utc}.000000Z"]
    subprocess.run(cmd + ["-movflags", "+faststart", "-f", "mp4", str(path)], check=True, capture_output=True)
    return path


def make_jpeg(path: Path, *, dto: str | None = "2022:01:30 12:31:00") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=1",
                    "-frames:v", "1", "-f", "image2", str(path)], check=True, capture_output=True)
    if dto and HAVE_EXIFTOOL:
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-DateTimeOriginal={dto}", "-Make=DJI",
                        "-Model=FC4170", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def media_tools():
    if not (HAVE_FFMPEG and HAVE_EXIFTOOL):
        pytest.skip("ffmpeg/ffprobe/exiftool not installed")
    return {"clip": make_clip, "jpeg": make_jpeg}
