from pathlib import Path

from cull import items as it


def touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def names(groups):
    return sorted((g.kind, tuple(g.files)) for g in groups)


def test_dji_video_with_companions(tmp_path):
    for n in ("DJI_0054.MP4", "DJI_0054.SRT", "DJI_0054.LRF", "DJI_0054.MP4.xmp"):
        touch(tmp_path / "30Jan" / n)
    groups, orphans = it.walk("Cine", tmp_path)
    assert names(groups) == [("video", ("30Jan/DJI_0054.MP4", "30Jan/DJI_0054.MP4.xmp", "30Jan/DJI_0054.LRF",
                                        "30Jan/DJI_0054.SRT"))]
    assert groups[0].id == "Cine/30Jan/DJI_0054.MP4" and orphans == []


def test_dng_jpg_pair_is_one_photo_with_jpg_first(tmp_path):
    touch(tmp_path / "DJI_0060.DNG")
    touch(tmp_path / "DJI_0060.JPG")
    groups, _ = it.walk("Cine", tmp_path)
    assert names(groups) == [("photo", ("DJI_0060.JPG", "DJI_0060.DNG"))]


def test_live_photo_is_one_item(tmp_path):
    touch(tmp_path / "IMG_1.HEIC")
    touch(tmp_path / "IMG_1.MOV")
    touch(tmp_path / "IMG_1.AAE")
    groups, _ = it.walk("phone", tmp_path)
    assert names(groups) == [("photo", ("IMG_1.HEIC", "IMG_1.MOV", "IMG_1.AAE"))]


def test_same_stem_video_and_raw_still_are_separate(tmp_path):
    touch(tmp_path / "DJI_0001.MP4")
    touch(tmp_path / "DJI_0001.SRT")
    touch(tmp_path / "DJI_0001.DNG")
    groups, _ = it.walk("s", tmp_path)
    assert names(groups) == [("photo", ("DJI_0001.DNG",)), ("video", ("DJI_0001.MP4", "DJI_0001.SRT"))]


def test_orphans_noise_symlinks_and_state_dirs(tmp_path):
    touch(tmp_path / "DJI_0099.LRF")
    touch(tmp_path / "._DJI_0001.MP4")
    touch(tmp_path / ".DS_Store")
    touch(tmp_path / "notes.pdf")
    touch(tmp_path / "_cull" / "thumbs" / "x.jpg")
    touch(tmp_path / ".hidden" / "y.jpg")
    real = touch(tmp_path / "real.mp4")
    (tmp_path / "link.mp4").symlink_to(real)
    groups, orphans = it.walk("s", tmp_path)
    assert names(groups) == [("video", ("real.mp4",))]
    assert orphans == ["DJI_0099.LRF"]


def test_register_sources_is_order_independent(tmp_path):
    a, b = tmp_path / "x" / "Cine", tmp_path / "y" / "Cine"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    first = it.register_sources({}, [a, b])
    assert first == {"Cine": str(a.resolve()), "Cine-2": str(b.resolve())}
    assert it.register_sources(first, [b, a]) == first
    c = tmp_path / "audiq7"
    c.mkdir()
    assert it.register_sources(first, [c])["audiq7"] == str(c.resolve())


def test_fingerprint_changes_with_content(tmp_path):
    p = touch(tmp_path / "a.mp4", b"one")
    fp1 = it.fingerprint(tmp_path, ["a.mp4"])
    assert fp1 == it.fingerprint(tmp_path, ["a.mp4"]) and len(fp1) == 16
    p.write_bytes(b"three")
    assert it.fingerprint(tmp_path, ["a.mp4"]) != fp1
