"""Phase Y.5 — ffprobe parsing + transcode policy (pure-Python side).

No ffmpeg/ffprobe invocation in these tests; we stub `_ffprobe_json`
so the logic for rotation/duration/codec-classification is exercised
without requiring the binary on the test runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from immy import video as video_mod


def _stub_ffprobe(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(video_mod, "_ffprobe_json", lambda p: payload)


def test_probe_reads_dimensions_and_duration(monkeypatch):
    _stub_ffprobe(monkeypatch, {
        "streams": [
            {"codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "14.269989"},
    })
    info = video_mod.probe(Path("fake.mp4"))
    assert info.width == 1920
    assert info.height == 1080
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.container_ext == ".mp4"
    assert abs(info.duration_s - 14.269989) < 1e-6


def test_probe_applies_side_data_rotation(monkeypatch):
    """iPhone portrait clips: coded 1920x1080 + rotation=-90 → 1080x1920."""
    _stub_ffprobe(monkeypatch, {
        "streams": [
            {"codec_type": "video", "codec_name": "hevc",
             "width": 1920, "height": 1080,
             "side_data_list": [{"rotation": -90}]},
        ],
        "format": {"duration": "8.0"},
    })
    info = video_mod.probe(Path("portrait.mov"))
    assert (info.width, info.height) == (1080, 1920)


def test_probe_legacy_tags_rotate(monkeypatch):
    _stub_ffprobe(monkeypatch, {
        "streams": [
            {"codec_type": "video", "codec_name": "h264",
             "width": 640, "height": 480,
             "tags": {"rotate": "90"}},
        ],
        "format": {"duration": "1.0"},
    })
    info = video_mod.probe(Path("old.mov"))
    assert (info.width, info.height) == (480, 640)


def test_probe_falls_back_to_stream_duration(monkeypatch):
    _stub_ffprobe(monkeypatch, {
        "streams": [
            {"codec_type": "video", "codec_name": "h264",
             "width": 100, "height": 100, "duration": "2.5"},
        ],
        "format": {},
    })
    info = video_mod.probe(Path("x.mp4"))
    assert info.duration_s == 2.5


def test_probe_raises_when_no_video_stream(monkeypatch):
    _stub_ffprobe(monkeypatch, {
        "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        "format": {},
    })
    with pytest.raises(video_mod.VideoProbeError):
        video_mod.probe(Path("audio.m4a"))


def test_format_duration_pads_to_millis():
    assert video_mod.format_duration(0) == "00:00:00.000"
    assert video_mod.format_duration(3661.5) == "01:01:01.500"
    assert video_mod.format_duration(14.269989).startswith("00:00:14.2")


def test_needs_transcode_matches_required_policy():
    make = lambda **kw: video_mod.VideoInfo(
        width=1280, height=720, duration_s=5.0,
        video_codec="h264", audio_codec="aac", container_ext=".mp4",
        **kw,
    )
    # Fully web-safe → no transcode.
    assert video_mod.needs_transcode(make()) is False
    # Wrong container.
    assert video_mod.needs_transcode(
        video_mod.VideoInfo(1280, 720, 5.0, "h264", "aac", ".mkv"),
    ) is True
    # Wrong video codec.
    assert video_mod.needs_transcode(
        video_mod.VideoInfo(1280, 720, 5.0, "hevc", "aac", ".mp4"),
    ) is True
    # Taller than target → downscale.
    assert video_mod.needs_transcode(
        video_mod.VideoInfo(3840, 2160, 5.0, "h264", "aac", ".mp4"),
    ) is True
    # Silent h264/mp4 is fine.
    assert video_mod.needs_transcode(
        video_mod.VideoInfo(640, 360, 2.0, "h264", "", ".mp4"),
    ) is False
