import subprocess
from pathlib import Path

from cull import media


def test_probe_and_playable(tmp_path, media_tools):
    h264 = media_tools["clip"](tmp_path / "a.MP4")
    p = media.ffprobe(h264)
    assert p.codec == "h264" and (p.width, p.height) == (320, 240) and abs(p.duration - 2.0) < 0.2
    assert media.playable(p.codec, p.width, p.height, h264)
    assert not media.playable("hevc", 320, 240, h264)
    assert not media.playable("h264", 3840, 2160, h264)
    assert media.playable("h264", 1080, 1920, h264)          # portrait 1080p
    assert not media.playable("h264", 320, 240, Path("a.avi"))
    assert media.ffprobe(tmp_path / "missing.mp4") is None


def test_proxy_from_unplayable_master(tmp_path, media_tools):
    src = media_tools["clip"](tmp_path / "m.MP4", codec="mpeg4", size="640x480")
    out = tmp_path / "px" / "m.mp4"
    assert media.make_proxy(src, out)
    p = media.ffprobe(out)
    assert p.codec == "h264" and p.height == 480 and not list(out.parent.glob("._*"))


def test_proxy_failure_leaves_nothing(tmp_path, media_tools):
    bad = tmp_path / "bad.MP4"
    bad.write_bytes(b"not a video")
    out = tmp_path / "px" / "bad.mp4"
    assert not media.make_proxy(bad, out)
    assert not out.exists() and not list(out.parent.glob("._*"))


def test_thumb_from_video_and_image(tmp_path, media_tools):
    clip = media_tools["clip"](tmp_path / "a.MP4")
    assert media.make_thumb(clip, tmp_path / "t" / "a.jpg", at=0.5)
    jpg = media_tools["jpeg"](tmp_path / "b.JPG")
    assert media.make_thumb(jpg, tmp_path / "t" / "b.jpg")
    assert media.ffprobe(tmp_path / "t" / "a.jpg").width == 320


def test_preview_falls_back_to_ffmpeg(tmp_path, media_tools):
    raw = tmp_path / "x.DNG"  # a TIFF under a raw name: no embedded preview, ffmpeg decodes it
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=1",
                    "-frames:v", "1", "-f", "image2", "-c:v", "tiff", str(raw)], check=True, capture_output=True)
    out = tmp_path / "pv" / "x.jpg"
    assert media.make_preview(raw, out) and out.stat().st_size > 0
