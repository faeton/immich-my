"""Tests for Insta360 master/proxy helpers."""

from __future__ import annotations

from pathlib import Path

from immy import insta360


def test_classify_master():
    hit = insta360.classify(Path("/trip/VID_20240101_120000_00_001.insv"))
    assert hit is not None
    role, key = hit
    assert role == "master"
    assert key.timestamp == "20240101_120000"
    assert key.serial == "001"


def test_classify_proxy_lrv_ext():
    hit = insta360.classify(Path("/trip/LRV_20240101_120000_01_001.lrv"))
    assert hit is not None
    role, _ = hit
    assert role == "proxy"


def test_classify_proxy_insv_ext():
    # Some models write the LRV payload into a .insv container — the
    # `LRV` prefix is authoritative, not the extension.
    hit = insta360.classify(Path("/trip/LRV_20240101_120000_01_001.insv"))
    assert hit is not None
    role, _ = hit
    assert role == "proxy"


def test_classify_unknown_returns_none():
    assert insta360.classify(Path("/trip/IMG_20240101_120000.jpg")) is None
    assert insta360.classify(Path("/trip/DJI_0001.mp4")) is None


def test_build_proxy_index_pairs_masters_and_proxies():
    paths = [
        Path("/trip/VID_20240101_120000_00_001.insv"),  # master lens A
        Path("/trip/VID_20240101_120000_10_001.insv"),  # master lens B
        Path("/trip/LRV_20240101_120000_01_001.lrv"),   # shared proxy
        Path("/trip/VID_20240102_130000_00_002.insv"),  # second recording, no proxy
    ]
    idx = insta360.build_proxy_index(paths)
    assert idx == {
        ("20240101_120000", "001"): Path("/trip/LRV_20240101_120000_01_001.lrv"),
    }


def test_proxy_for_returns_match():
    paths = [
        Path("/trip/VID_20240101_120000_00_001.insv"),
        Path("/trip/LRV_20240101_120000_01_001.lrv"),
    ]
    idx = insta360.build_proxy_index(paths)
    master = Path("/trip/VID_20240101_120000_10_001.insv")
    assert insta360.proxy_for(master, idx) == Path("/trip/LRV_20240101_120000_01_001.lrv")


def test_proxy_for_master_without_proxy():
    idx = insta360.build_proxy_index([Path("/trip/VID_20240101_120000_00_001.insv")])
    assert insta360.proxy_for(Path("/trip/VID_20240101_120000_00_001.insv"), idx) is None


def test_proxy_for_non_master_returns_none():
    idx = insta360.build_proxy_index(
        [Path("/trip/LRV_20240101_120000_01_001.lrv")],
    )
    # Passing a proxy itself is not a "master" query — returns None
    # so callers don't accidentally redirect a proxy asset's derivatives
    # to itself via this lookup path.
    proxy = Path("/trip/LRV_20240101_120000_01_001.lrv")
    assert insta360.proxy_for(proxy, idx) is None
    # Non-Insta360 files likewise.
    assert insta360.proxy_for(Path("/trip/IMG_0001.jpg"), idx) is None
