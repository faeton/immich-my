from __future__ import annotations

from pathlib import Path

import pytest

from immy.exif import ExifRow
from immy.rules.bloat_candidate import _propose


def _row(path: Path, **raw) -> ExifRow:
    return ExifRow(path=path, raw=raw)


def _h264_1080p(bitrate: int) -> dict:
    return {
        "QuickTime:CompressorID": "avc1",
        "QuickTime:ImageWidth": 1920,
        "QuickTime:ImageHeight": 1080,
        "QuickTime:VideoFrameRate": 30,
        "Composite:AvgBitrate": bitrate,
    }


def _hevc_4k(bitrate: int) -> dict:
    return {
        "QuickTime:CompressorID": "hvc1",
        "QuickTime:ImageWidth": 3840,
        "QuickTime:ImageHeight": 2160,
        "QuickTime:VideoFrameRate": 30,
        "Composite:AvgBitrate": bitrate,
    }


def test_flags_fat_h264(tmp_path: Path):
    # 1080p30 at 15 Mbps = 0.241 bpp → fat
    row = _row(tmp_path / "edit.mp4", **_h264_1080p(15_000_000))
    findings = _propose([row], tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "bloat-candidate"
    assert findings[0].action == "note"
    assert "fat" in findings[0].reason


def test_flags_obscene_hevc(tmp_path: Path):
    # 4K30 HEVC at 200 Mbps = 0.803 bpp → obscene
    row = _row(tmp_path / "export.mp4", **_hevc_4k(200_000_000))
    findings = _propose([row], tmp_path)
    assert len(findings) == 1
    assert "obscene" in findings[0].reason


def test_skips_sane_h264(tmp_path: Path):
    # 1080p30 at 6 Mbps = 0.096 bpp → sane
    row = _row(tmp_path / "fine.mp4", **_h264_1080p(6_000_000))
    assert _propose([row], tmp_path) == []


def test_skips_camera_native_prefix(tmp_path: Path):
    # DJI_ prefix → never flag regardless of bitrate.
    row = _row(tmp_path / "DJI_0042.MP4", **_h264_1080p(40_000_000))
    assert _propose([row], tmp_path) == []


def test_skips_gopro_prefix(tmp_path: Path):
    row = _row(tmp_path / "GX010001.MP4", **_hevc_4k(100_000_000))
    assert _propose([row], tmp_path) == []


def test_skips_date_stamped_phone_filename(tmp_path: Path):
    # Android/Pixel raw camera output — VID_YYYYMMDD_HHMMSS.mp4
    row = _row(tmp_path / "VID_20260401_120000.mp4", **_h264_1080p(30_000_000))
    assert _propose([row], tmp_path) == []


def test_skips_prores_mov(tmp_path: Path):
    row = _row(
        tmp_path / "delivery.mov",
        **{
            "QuickTime:CompressorID": "apch",  # ProRes 422 HQ
            "QuickTime:ImageWidth": 3840, "QuickTime:ImageHeight": 2160,
            "QuickTime:VideoFrameRate": 24,
            "Composite:AvgBitrate": 500_000_000,  # huge but it's an edit source
        },
    )
    assert _propose([row], tmp_path) == []


def test_skips_insv_extension(tmp_path: Path):
    row = _row(
        tmp_path / "VID_20240101_120000_00_001.insv",
        **_hevc_4k(200_000_000),
    )
    assert _propose([row], tmp_path) == []


def test_skips_insta360_exported_mp4(tmp_path: Path):
    # 5.7K equirectangular MP4 export lands as .mp4 but Make=Insta360 —
    # per feedback memory, these are re-edit sources, never flag.
    row = _row(
        tmp_path / "reframed.mp4",
        **{
            "QuickTime:CompressorID": "hvc1",
            "QuickTime:ImageWidth": 5760, "QuickTime:ImageHeight": 2880,
            "QuickTime:VideoFrameRate": 30,
            "Composite:AvgBitrate": 120_000_000,
            "QuickTime:Make": "Insta360",
        },
    )
    assert _propose([row], tmp_path) == []


def test_skips_edit_folder(tmp_path: Path):
    edit_dir = tmp_path / "my-edit"
    edit_dir.mkdir()
    row = _row(edit_dir / "cut.mp4", **_h264_1080p(40_000_000))
    assert _propose([row], tmp_path) == []


def test_skips_non_video(tmp_path: Path):
    row = _row(tmp_path / "photo.jpg", **_h264_1080p(40_000_000))
    assert _propose([row], tmp_path) == []


def test_computes_bitrate_from_size_and_duration(tmp_path: Path):
    # Some containers omit AvgBitrate — derive from FileSize / Duration.
    row = _row(
        tmp_path / "derived.mp4",
        **{
            "QuickTime:CompressorID": "avc1",
            "QuickTime:ImageWidth": 1920, "QuickTime:ImageHeight": 1080,
            "QuickTime:VideoFrameRate": 30,
            "File:FileSize": 2_000_000_000,    # 2 GB
            "QuickTime:Duration": 600,          # 10 min
            # 2e9 * 8 / 600 = 26.67 Mbps → 0.429 bpp → obscene
        },
    )
    findings = _propose([row], tmp_path)
    assert len(findings) == 1
    assert "obscene" in findings[0].reason
