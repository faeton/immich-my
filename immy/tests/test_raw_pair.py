"""Tests for RAW master / camera-baked JPEG preview pairing."""

from __future__ import annotations

from pathlib import Path

from immy import raw


def test_classify_raw_dng():
    hit = raw.classify(Path("/trip/DJI_0400.DNG"))
    assert hit == ("raw", ("/trip", "dji_0400"))


def test_classify_raw_arw():
    assert raw.classify(Path("/trip/DSC01234.ARW"))[0] == "raw"


def test_classify_preview_jpg():
    hit = raw.classify(Path("/trip/DJI_0400.JPG"))
    assert hit == ("preview", ("/trip", "dji_0400"))


def test_classify_preview_heic():
    assert raw.classify(Path("/trip/IMG_0001.HEIC"))[0] == "preview"


def test_classify_unknown_returns_none():
    assert raw.classify(Path("/trip/DJI_0400.MP4")) is None
    assert raw.classify(Path("/trip/DJI_0400.SRT")) is None


def test_build_raw_index_collects_keys():
    paths = [
        Path("/trip/DJI_0400.DNG"),
        Path("/trip/DJI_0400.JPG"),
        Path("/trip/DSC01234.ARW"),
        Path("/trip/IMG_0500.JPG"),
    ]
    idx = raw.build_raw_index(paths)
    assert idx == {("/trip", "dji_0400"), ("/trip", "dsc01234")}


def test_is_paired_preview_pair_filtered():
    paths = [Path("/trip/DJI_0400.DNG"), Path("/trip/DJI_0400.JPG")]
    idx = raw.build_raw_index(paths)
    assert raw.is_paired_preview(Path("/trip/DJI_0400.JPG"), idx) is True


def test_is_paired_preview_orphan_jpg_kept():
    paths = [Path("/trip/IMG_0500.JPG")]
    idx = raw.build_raw_index(paths)
    assert raw.is_paired_preview(Path("/trip/IMG_0500.JPG"), idx) is False


def test_is_paired_preview_raw_never_filtered():
    paths = [Path("/trip/DJI_0400.DNG"), Path("/trip/DJI_0400.JPG")]
    idx = raw.build_raw_index(paths)
    assert raw.is_paired_preview(Path("/trip/DJI_0400.DNG"), idx) is False


def test_pairing_is_case_insensitive_on_extension_and_stem():
    paths = [Path("/trip/DJI_0400.dng"), Path("/trip/dji_0400.Jpg")]
    idx = raw.build_raw_index(paths)
    assert raw.is_paired_preview(Path("/trip/dji_0400.Jpg"), idx) is True


def test_pairing_is_directory_scoped():
    # Same stem in different folders must not pair.
    paths = [Path("/a/IMG_0001.DNG"), Path("/b/IMG_0001.JPG")]
    idx = raw.build_raw_index(paths)
    assert raw.is_paired_preview(Path("/b/IMG_0001.JPG"), idx) is False


def test_sony_arw_jpg_pair():
    paths = [Path("/trip/DSC01234.ARW"), Path("/trip/DSC01234.JPG")]
    idx = raw.build_raw_index(paths)
    assert raw.is_paired_preview(Path("/trip/DSC01234.JPG"), idx) is True
