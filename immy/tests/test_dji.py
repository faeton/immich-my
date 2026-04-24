"""Tests for DJI master/proxy pairing."""

from __future__ import annotations

from pathlib import Path

from immy import dji


def test_classify_master_mp4():
    hit = dji.classify(Path("/trip/DJI_20250614122012_0002_D.MP4"))
    assert hit is not None
    role, key = hit
    assert role == "master"
    assert key == ("/trip", "dji_20250614122012_0002_d")


def test_classify_master_mov():
    hit = dji.classify(Path("/trip/DJI_0001.MOV"))
    assert hit is not None
    assert hit[0] == "master"


def test_classify_proxy_lrf():
    hit = dji.classify(Path("/trip/DJI_20250614122012_0002_D.LRF"))
    assert hit is not None
    assert hit[0] == "proxy"


def test_classify_unknown_returns_none():
    assert dji.classify(Path("/trip/IMG_0001.jpg")) is None
    assert dji.classify(Path("/trip/VID_20240101_120000_00_001.insv")) is None


def test_build_proxy_index_pairs_by_stem():
    # Orphan LRFs are intentionally dropped from the index so they
    # stay in the ingest row list and the user sees them.
    paths = [
        Path("/trip/DJI_20250614122012_0002_D.MP4"),
        Path("/trip/DJI_20250614122012_0002_D.LRF"),
        Path("/trip/DJI_20250614122040_0003_D.MP4"),  # master, no LRF
        Path("/trip/DJI_20250614122100_0004_D.LRF"),  # orphan LRF
    ]
    idx = dji.build_proxy_index(paths)
    assert idx == {
        ("/trip", "dji_20250614122012_0002_d"): Path(
            "/trip/DJI_20250614122012_0002_D.LRF",
        ),
    }


def test_proxy_for_returns_sibling():
    paths = [
        Path("/trip/DJI_20250614122012_0002_D.MP4"),
        Path("/trip/DJI_20250614122012_0002_D.LRF"),
    ]
    idx = dji.build_proxy_index(paths)
    master = Path("/trip/DJI_20250614122012_0002_D.MP4")
    assert dji.proxy_for(master, idx) == Path(
        "/trip/DJI_20250614122012_0002_D.LRF",
    )


def test_proxy_for_master_without_sibling():
    idx = dji.build_proxy_index(
        [Path("/trip/DJI_20250614122012_0002_D.MP4")],
    )
    assert dji.proxy_for(
        Path("/trip/DJI_20250614122012_0002_D.MP4"), idx,
    ) is None


def test_proxy_for_non_master_returns_none():
    idx = dji.build_proxy_index(
        [Path("/trip/DJI_20250614122012_0002_D.LRF")],
    )
    # An LRF queried as if it were a master — must not self-reference.
    assert dji.proxy_for(
        Path("/trip/DJI_20250614122012_0002_D.LRF"), idx,
    ) is None
    assert dji.proxy_for(Path("/trip/IMG_0001.jpg"), idx) is None


def test_pairing_is_directory_scoped():
    # Same stem in different directories must not cross-pair (two
    # separate trips could both have a DJI_0001).
    paths = [
        Path("/trip-a/DJI_0001.MP4"),
        Path("/trip-b/DJI_0001.LRF"),
    ]
    idx = dji.build_proxy_index(paths)
    assert dji.proxy_for(Path("/trip-a/DJI_0001.MP4"), idx) is None


def test_is_paired_proxy_true_for_paired_lrf():
    paths = [
        Path("/trip/DJI_0001.MP4"),
        Path("/trip/DJI_0001.LRF"),
    ]
    idx = dji.build_proxy_index(paths)
    assert dji.is_paired_proxy(Path("/trip/DJI_0001.LRF"), idx) is True


def test_is_paired_proxy_false_for_orphan_lrf():
    # Orphan LRF with no matching master is dropped from the index,
    # so is_paired_proxy returns False — the caller keeps it in the
    # row list so the user can see the stray proxy.
    paths = [Path("/trip/DJI_0001.LRF")]
    idx = dji.build_proxy_index(paths)
    assert dji.is_paired_proxy(Path("/trip/DJI_0001.LRF"), idx) is False
