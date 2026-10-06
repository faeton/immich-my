"""Takeout JSON companion lookup: every naming shape Google produces."""

from __future__ import annotations

import json
from pathlib import Path

from immy.dedup.engine import _google_json_companion


def _json(folder: Path, name: str, title: str, ts: int) -> None:
    (folder / name).write_text(json.dumps(
        {"title": title, "photoTakenTime": {"timestamp": str(ts)}}))


def _ts(folder: Path, media: str) -> int | None:
    j = _google_json_companion(folder / media)
    return int(j["photoTakenTime"]["timestamp"]) if j else None


def test_plain_names(tmp_path: Path) -> None:
    _json(tmp_path, "A.JPG.json", "A.JPG", 1)
    _json(tmp_path, "B.JPG.supplemental-metadata.json", "B.JPG", 2)
    assert _ts(tmp_path, "A.JPG") == 1
    assert _ts(tmp_path, "B.JPG") == 2


def test_duplicate_counter_moves_to_the_end(tmp_path: Path) -> None:
    _json(tmp_path, "IMG_1.JPG.supplemental-metadata.json", "IMG_1.JPG", 10)
    _json(tmp_path, "IMG_1.JPG.supplemental-metadata(1).json", "IMG_1.JPG", 11)
    _json(tmp_path, "IMG_1.JPG.supplemental-metadata(2).json", "IMG_1.JPG", 12)
    assert _ts(tmp_path, "IMG_1.JPG") == 10
    assert _ts(tmp_path, "IMG_1(1).JPG") == 11
    assert _ts(tmp_path, "IMG_1(2).JPG") == 12


def test_live_photo_video_uses_its_stills_json(tmp_path: Path) -> None:
    _json(tmp_path, "IMG_7.HEIC.supplemental-metadata(1).json", "IMG_7.HEIC", 70)
    assert _ts(tmp_path, "IMG_7(1).MP4") == 70
    # A still never borrows a video's JSON.
    _json(tmp_path, "IMG_8.MOV.supplemental-metadata.json", "IMG_8.MOV", 80)
    assert _ts(tmp_path, "IMG_8(1).JPG") is None


def test_same_extension_wins_over_the_still(tmp_path: Path) -> None:
    _json(tmp_path, "IMG_9.HEIC.supplemental-metadata(1).json", "IMG_9.HEIC", 90)
    _json(tmp_path, "IMG_9.MP4.supplemental-metadata(1).json", "IMG_9.MP4", 91)
    assert _ts(tmp_path, "IMG_9(1).MP4") == 91


def test_edited_copy_uses_the_originals_json(tmp_path: Path) -> None:
    _json(tmp_path, "IMG_5.JPG.supplemental-metadata.json", "IMG_5.JPG", 50)
    assert _ts(tmp_path, "IMG_5-edited.JPG") == 50


def test_truncated_json_name_is_found_by_title(tmp_path: Path) -> None:
    long = "Screenshot_2021-12-16-14-01-59-821_com.example.app"
    _json(tmp_path, "Screenshot_2021-12-16-14-01-59-821_com.ex(1).json", long + ".jpg", 33)
    assert _ts(tmp_path, long + "(1).jpg") == 33


def test_title_with_its_own_parentheses(tmp_path: Path) -> None:
    _json(tmp_path, "shot (1).png.supplemental-metadata(1).json", "shot (1).png", 44)
    assert _ts(tmp_path, "shot (1)(1).png") == 44


def test_a_different_photo_sharing_a_prefix_is_not_used(tmp_path: Path) -> None:
    # IMG_12.JPG must not be dated from IMG_123.JPG's JSON.
    _json(tmp_path, "IMG_123.JPG.supplemental-metadata.json", "IMG_123.JPG", 123)
    assert _ts(tmp_path, "IMG_12.JPG") is None


def test_brackets_in_names_do_not_break_the_glob(tmp_path: Path) -> None:
    _json(tmp_path, "a[b].JPG.supplemental-metadata(1).json", "a[b].JPG", 7)
    assert _ts(tmp_path, "a[b](1).JPG") == 7


def test_ambiguous_match_returns_nothing(tmp_path: Path) -> None:
    # Two stills with the same stem and counter: no way to tell → no date
    # rather than a guess.
    _json(tmp_path, "IMG_3.HEIC.supplemental-metadata(1).json", "IMG_3.HEIC", 1)
    _json(tmp_path, "IMG_3.JPG.supplemental-metadata(1).json", "IMG_3.JPG", 2)
    assert _ts(tmp_path, "IMG_3(1).MP4") is None


def test_same_kind_other_extension_is_a_last_resort(tmp_path: Path) -> None:
    _json(tmp_path, "IMG_4.jpeg.supplemental-metadata(1).json", "IMG_4.jpeg", 4)
    assert _ts(tmp_path, "IMG_4(1).jpg") == 4
    _json(tmp_path, "IMG_6.mp4.supplemental-metadata(1).json", "IMG_6.mp4", 6)
    assert _ts(tmp_path, "IMG_6(1).HEIC") is None


def test_literal_parentheses_in_a_real_name(tmp_path: Path) -> None:
    # The file is really called "shot (1).png"; its JSON carries no counter.
    _json(tmp_path, "shot (1).png.json", "shot (1).png", 9)
    assert _ts(tmp_path, "shot (1).png") == 9
    (tmp_path / "shot (1).png.json").unlink()
    _json(tmp_path, "shot (1).png.supplemental-metadata.json", "shot (1).png", 10)
    assert _ts(tmp_path, "shot (1).png") == 10


def test_literal_edited_in_a_real_name(tmp_path: Path) -> None:
    _json(tmp_path, "trip-edited.jpg.supplemental-metadata.json", "trip-edited.jpg", 11)
    assert _ts(tmp_path, "trip-edited.jpg") == 11


def test_exact_json_with_another_title_is_not_trusted(tmp_path: Path) -> None:
    _json(tmp_path, "IMG_7.JPG.json", "SOMETHING_ELSE.JPG", 1)
    assert _ts(tmp_path, "IMG_7.JPG") is None


def test_exact_lookup_never_falls_back(tmp_path: Path) -> None:
    from immy.dedup.engine import takeout_json_exact
    _json(tmp_path, "IMG_5.HEIC.supplemental-metadata.json", "IMG_5.HEIC", 5)
    assert takeout_json_exact(tmp_path / "IMG_5.HEIC")["title"] == "IMG_5.HEIC"
    assert takeout_json_exact(tmp_path / "IMG_5.MP4") is None    # no Live Photo borrowing
    assert takeout_json_exact(tmp_path / "IMG_5.heic") is None   # case matters


def test_zone_at_rejects_null_island_and_nonsense() -> None:
    from immy.dedup.engine import zone_at
    assert zone_at(0.0, 0.0) is None
    assert zone_at(0.0005, -0.0002) is None
    assert zone_at(95.0, 10.0) is None
    assert zone_at(10.0, 200.0) is None
    assert zone_at(41.88, -87.63).key == "America/Chicago"
