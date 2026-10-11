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


def test_duration_ms_is_integer_milliseconds():
    # Immich 3.x `asset.duration` is int4 milliseconds.
    assert video_mod.duration_ms(0) == 0
    assert video_mod.duration_ms(3661.5) == 3_661_500
    assert video_mod.duration_ms(14.269989) == 14_270
    # Matches what Immich's own scanner wrote for this clip.
    assert video_mod.duration_ms(44.8448) == 44_845
    assert video_mod.duration_ms(-1.0) == 0


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    (12_500, 12_500),
    ("00:00:12.500", 12_500),       # legacy immy offline cache / journal
    ("01:01:01.5", 3_661_500),
    ("0:00:04", 4_000),
    ("4000", 4_000),
    ("garbage", None),
    ("", None),
])
def test_normalize_duration_ms_accepts_legacy_strings(raw, expected):
    assert video_mod.normalize_duration_ms(raw) == expected


def _n5_config(**ffmpeg):
    base = {"transcode": "required", "targetResolution": "720",
            "acceptedVideoCodecs": ["h264", "hevc", "av1"], "acceptedAudioCodecs": ["aac", "mp3", "libopus"],
            "acceptedContainers": ["mov", "ogg", "webm"], "targetVideoCodec": "h264", "maxBitrate": "0"}
    base.update(ffmpeg)
    return {"ffmpeg": base}


def _info(w=2896, h=1876, vcodec="h264", acodec="", ext=".mp4", pix="yuv420p", bitrate=20_000_000,
          fmt="mov,mp4,m4a,3gp,3g2,mj2", long="QuickTime / MOV"):
    return video_mod.VideoInfo(w, h, 60.0, vcodec, acodec, ext, pix_fmt=pix, bitrate=bitrate,
                               format_name=fmt, format_long_name=long)


def test_transcode_policy_from_live_config_mirrors_immich():
    """The Tesla case that motivated this: n5 runs 'required' with h264/hevc/av1
    accepted, so a 2.9K H.264 camera file and an AV1 drone file are served as
    is — immy must not pre-encode them. The built-in rule (no policy) would."""
    p = video_mod.TranscodePolicy.from_system_config(_n5_config())
    assert p.transcode == "required" and p.accepted_video_codecs == ("h264", "hevc", "av1")
    assert p.max_bitrate_bps == 0 and "720p" in p.describe()
    assert video_mod.needs_transcode(_info(), p) is False
    assert video_mod.needs_transcode(_info()) is True                       # built-in rule: taller than 720
    assert video_mod.needs_transcode(_info(vcodec="av1"), p) is False
    assert video_mod.needs_transcode(_info(vcodec="av1")) is True
    # what Immich WOULD still transcode under 'required'
    assert video_mod.immich_transcode_reason(_info(vcodec="mpeg4"), p) == "video mpeg4 not accepted"
    assert video_mod.immich_transcode_reason(_info(pix="yuv422p10le"), p).startswith("pixel format")
    assert video_mod.immich_transcode_reason(_info(acodec="pcm_s16le"), p) == "audio pcm_s16le not accepted"
    assert "remuxed" in video_mod.immich_transcode_reason(
        _info(ext=".mkv", fmt="matroska,webm", long="Matroska / WebM"),
        video_mod.TranscodePolicy.from_system_config(_n5_config(acceptedContainers=["mov"])))
    # the other policies
    opt = video_mod.TranscodePolicy.from_system_config(_n5_config(transcode="optimal"))
    assert video_mod.immich_transcode_reason(_info(), opt) == "2896×1876 above 720p"
    assert video_mod.immich_transcode_reason(_info(w=1280, h=720), opt) == ""
    assert video_mod.immich_transcode_reason(_info(w=1920, h=1080), video_mod.TranscodePolicy.from_system_config(
        _n5_config(transcode="optimal", targetResolution="original"))) == ""
    br = video_mod.TranscodePolicy.from_system_config(_n5_config(transcode="bitrate", maxBitrate="8M"))
    assert br.max_bitrate_bps == 8_000_000
    assert "bitrate" in video_mod.immich_transcode_reason(_info(bitrate=20_000_000), br)
    assert video_mod.immich_transcode_reason(_info(bitrate=5_000_000), br) == ""
    assert video_mod.immich_transcode_reason(_info(), video_mod.TranscodePolicy.from_system_config(
        _n5_config(transcode="all"))) == "policy 'all'"
    assert video_mod.immich_transcode_reason(_info(vcodec="mpeg4"), video_mod.TranscodePolicy.from_system_config(
        _n5_config(transcode="disabled"))) == ""
    # defaults when the server answers nothing useful
    d = video_mod.TranscodePolicy.from_system_config({})
    assert d.transcode == "required" and d.accepted_video_codecs == ("h264",)


def test_transcode_policy_edge_cases_match_immich(monkeypatch, tmp_path):
    # JavaScript parseInt semantics for maxBitrate: '8.5M' is 8 Mbit/s upstream
    assert video_mod._parse_bitrate_bps("8.5M") == 8_000_000
    assert video_mod._parse_bitrate_bps("8000k") == 8_000_000
    assert video_mod._parse_bitrate_bps("12abc") == 12
    assert video_mod._parse_bitrate_bps("abc") == 0 and video_mod._parse_bitrate_bps(None) == 0
    assert video_mod._parse_bitrate_bps("0") == 0
    # an EMPTY accept list is a real setting (accept nothing), not "use the default"
    empty = video_mod.TranscodePolicy.from_system_config(_n5_config(acceptedVideoCodecs=[]))
    assert empty.accepted_video_codecs == ()
    assert video_mod.immich_transcode_reason(_info(), empty) == "video h264 not accepted"
    no_audio = video_mod.TranscodePolicy.from_system_config(_n5_config(acceptedAudioCodecs=[]))
    assert video_mod.immich_transcode_reason(_info(acodec="aac"), no_audio) == "audio aac not accepted"
    assert video_mod.immich_transcode_reason(_info(acodec=""), no_audio) == ""    # silent: no audio decision
    # the stream's bitrate only — a missing stream bit_rate is 0, never the container's
    monkeypatch.setattr(video_mod, "_ffprobe_json", lambda p: {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
                     "pix_fmt": "yuv420p"}],
        "format": {"duration": "5.0", "bit_rate": "20000000", "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                   "format_long_name": "QuickTime / MOV"},
    })
    info = video_mod.probe(tmp_path / "clip.mp4")
    assert info.bitrate == 0 and info.pix_fmt == "yuv420p" and info.format_long_name == "QuickTime / MOV"
    br = video_mod.TranscodePolicy.from_system_config(_n5_config(transcode="bitrate", maxBitrate="8M"))
    assert video_mod.immich_transcode_reason(info, br) == ""
    # Grok round 2: unmapped long names compare the WHOLE format_name; an audio
    # stream ffprobe cannot name is a stream, not silence; h265 is hevc
    monkeypatch.setattr(video_mod, "_ffprobe_json", lambda p: {
        "streams": [{"codec_type": "video", "codec_name": "h265", "width": 1280, "height": 720},
                    {"codec_type": "audio"}],
        "format": {"duration": "5.0", "format_name": "avi", "format_long_name": "AVI (Audio Video Interleaved)"},
    })
    odd = video_mod.probe(tmp_path / "clip.avi")
    assert odd.video_codec == "hevc" and odd.audio_codec == "unknown"
    p = video_mod.TranscodePolicy.from_system_config(_n5_config())
    assert video_mod.immich_transcode_reason(odd, p) == "audio unknown not accepted"
    assert video_mod._immich_container(odd) == "avi"
    assert "remuxed" in video_mod.immich_transcode_reason(_info(acodec="", fmt="avi", long="AVI (Audio Video Interleaved)"), p)
    assert video_mod._immich_container(_info(fmt="mpegts,ts", long="MPEG-TS (MPEG-2 Transport Stream)")) == "mpegts,ts"


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
