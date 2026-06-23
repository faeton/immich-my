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
        (False, "20240101_120000", "001"):
            Path("/trip/LRV_20240101_120000_01_001.lrv"),
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


def test_dewarp_vf_lens_00_clockwise():
    vf = insta360.dewarp_vf(Path("/trip/VID_20240101_120000_00_001.insv"))
    assert vf is not None
    assert "v360=input=fisheye:output=flat" in vf
    assert "ih_fov=200:iv_fov=200" in vf
    assert vf.endswith(",transpose=1")


def test_dewarp_vf_lens_10_cw_vflip():
    vf = insta360.dewarp_vf(Path("/trip/VID_20240101_120000_10_001.insv"))
    assert vf is not None
    assert vf.endswith(",transpose=3")


def test_dewarp_vf_none_for_proxy_and_unknown():
    # Proxies are already stitched equirect — de-warping them would
    # double-warp. Returns None so the caller skips the filter.
    assert insta360.dewarp_vf(
        Path("/trip/LRV_20240101_120000_01_001.lrv"),
    ) is None
    assert insta360.dewarp_vf(Path("/trip/IMG_0001.jpg")) is None
    # Unknown lens code (e.g. future model) — no mapping, returns None.
    assert insta360.dewarp_vf(
        Path("/trip/VID_20240101_120000_99_001.insv"),
    ) is None


def test_classify_pro_master_and_proxy():
    # GO2 PureView/PRO mode prefixes the scheme with `PRO_`; the role
    # still comes from the VID/LRV token, not the prefix.
    m = insta360.classify(Path("/trip/PRO_VID_20221004_101951_00_006.mp4"))
    assert m is not None and m[0] == "master"
    assert m[1].timestamp == "20221004_101951" and m[1].serial == "006"
    p = insta360.classify(Path("/trip/PRO_LRV_20221004_101951_01_006.mp4"))
    assert p is not None and p[0] == "proxy"


def test_pro_master_pairs_with_pro_proxy():
    paths = [
        Path("/trip/PRO_VID_20221004_101951_00_006.mp4"),
        Path("/trip/PRO_LRV_20221004_101951_01_006.mp4"),
    ]
    idx = insta360.build_proxy_index(paths)
    master = Path("/trip/PRO_VID_20221004_101951_00_006.mp4")
    assert insta360.proxy_for(master, idx) == \
        Path("/trip/PRO_LRV_20221004_101951_01_006.mp4")


def test_pro_and_plain_never_cross_pair():
    # A plain VID and a PRO_LRV that happen to share timestamp+serial must
    # NOT pair — the `pro` bit keys them apart (Codex review).
    paths = [
        Path("/trip/VID_20240101_120000_00_001.insv"),       # plain master
        Path("/trip/PRO_LRV_20240101_120000_01_001.mp4"),    # PRO proxy, same ts/serial
    ]
    idx = insta360.build_proxy_index(paths)
    assert insta360.proxy_for(
        Path("/trip/VID_20240101_120000_00_001.insv"), idx) is None


def test_dewarp_vf_none_for_mp4_master():
    # X-series de-warp is for `.insv` only; a GO2 `.mp4` (handled by
    # go2_dewarp_vf) must not get the lens-rotation filter (Grok review).
    assert insta360.dewarp_vf(
        Path("/trip/VID_20220919_140654_00_026.mp4")) is None


def test_go2_dewarp_rejects_partial_token():
    # `PRO_VIDEO_EXPORT.mp4` starts with PRO_VID but isn't canonical.
    assert insta360.go2_dewarp_vf(Path("/trip/PRO_VIDEO_EXPORT.mp4")) is None


def test_go2_dewarp_for_pro_vid_and_lrv():
    # PRO master → flat de-warp, NO transpose (GO2 is recorded upright).
    vf = insta360.go2_dewarp_vf(
        Path("/trip/PRO_VID_20221004_101951_00_006.mp4"))
    assert vf is not None
    assert "v360=input=fisheye:output=flat" in vf
    assert "transpose" not in vf
    # PRO LRV is the stack primary (its own tile is shown) — must also
    # de-warp, with the identical filter.
    assert insta360.go2_dewarp_vf(
        Path("/trip/PRO_LRV_20221004_101951_01_006.mp4")) == vf
    # Plain VID/LRV are already reframed 16:9 / equirect → never de-warped.
    assert insta360.go2_dewarp_vf(
        Path("/trip/VID_20220919_140654_00_026.mp4")) is None
    assert insta360.go2_dewarp_vf(
        Path("/trip/LRV_20240101_120000_01_001.lrv")) is None
    assert insta360.go2_dewarp_vf(Path("/trip/IMG_0001.jpg")) is None


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
