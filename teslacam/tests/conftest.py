from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_clip(path: Path, *, seconds: float = 2.0, size: str = "320x240", utc: str = "2026-09-09T17:06:19",
              fps: int = 10) -> None:
    """A tiny H.264 clip with a creation_time, like a Tesla segment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "5",
                    "-metadata", f"creation_time={utc}.000000Z", "-movflags", "+faststart", str(path)],
                   check=True, capture_output=True)


@pytest.fixture
def ffmpeg():
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not installed")
    return make_clip


@pytest.fixture
def dumps(tmp_path: Path, ffmpeg) -> Path:
    """A dumps root with: Dump1/SavedClips/<ev> (4-cam, 3 segs, event.json),
    Dump1/SentryClips/<ev> (no event.json), Dump2 flat 2024 layout, and
    Dump1/RecentClips with two drives separated by a gap."""
    root = tmp_path / "dumps"
    cams4 = ("front", "back", "left_repeater", "right_repeater")
    # Saved: segments at 19:06:19, 19:07:21, 19:08:23 (2 s each in the fixture)
    ev = root / "Dump1" / "SavedClips" / "2026-09-09_19-08-30"
    for i, ts in enumerate(("2026-09-09_19-06-19", "2026-09-09_19-07-21", "2026-09-09_19-08-23")):
        for cam in cams4:
            ffmpeg(ev / f"{ts}-{cam}.mp4", utc=f"2026-09-09T17:0{6 + i}:{19 + 2 * i:02d}")
    (ev / "event.json").write_text('{"timestamp":"2026-09-09T19:08:27","city":"Granada","est_lat":"37.1763",'
                                   '"est_lon":"-3.5978","reason":"user_interaction_honk","camera":"0"}')
    sen = root / "Dump1" / "SentryClips" / "2026-09-09_22-00-00"
    for cam in cams4:
        ffmpeg(sen / f"2026-09-09_21-59-00-{cam}.mp4", utc="2026-09-09T19:59:00")
    # flat 2024 layout
    flat = root / "Dump2" / "2024-05-14_14-34-55__prien-am-chiemsee__honk"
    for cam in cams4:
        ffmpeg(flat / f"2024-05-14_14-33-52-{cam}.mp4", utc="2024-05-14T12:33:52")
    (flat / "event.json").write_text('{"timestamp":"2024-05-14T14:34:45","city":"Prien am Chiemsee",'
                                     '"est_lat":"47.8599","est_lon":"12.3651","reason":"user_interaction_honk","camera":"0"}')
    # RecentClips: 15:00:16, 15:00:19 (contiguous) … gap … 16:00:00
    rc = root / "Dump1" / "RecentClips"
    for ts, utc in (("2025-08-01_15-00-16", "2025-08-01T14:00:16"), ("2025-08-01_15-00-19", "2025-08-01T14:00:19"),
                    ("2025-08-01_16-00-00", "2025-08-01T15:00:00")):
        for cam in ("front", "left_repeater"):
            ffmpeg(rc / f"{ts}-{cam}.mp4", utc=utc)
    # an excluded dump and a duplicate of the Saved event
    dup = root / "Dump3" / "SavedClips" / "2026-09-09_19-08-30"
    for f in ev.glob("*.mp4"):
        dup.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dup / f.name)
    return root
