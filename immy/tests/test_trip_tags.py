"""Tests for `immy/rules/trip_tags.py`'s per-file tag resolution — the logic
shared between the `trip-tags-from-notes` XMP rule (photos) and
`tagsync.py`'s native Immich Tag API push (videos, which never pick up XMP).
"""

from __future__ import annotations

from pathlib import Path

from immy.exif import ExifRow
from immy.rules.trip_tags import file_camera, tags_for_file


def _row(name: str, **raw) -> ExifRow:
    return ExifRow(path=Path(name), raw=raw)


# --- file_camera ------------------------------------------------------------

def test_file_camera_from_exif_make_model():
    row = _row("IMG_1.HEIC", **{"EXIF:Make": "Apple", "EXIF:Model": "iPhone 17 Pro"})
    assert file_camera(row) == "Apple iPhone 17 Pro"


def test_file_camera_dji_filename_fallback():
    row = _row("DJI_20260625121010_0030_D.MP4")
    assert file_camera(row) == "DJI"


def test_file_camera_gopro_filename_fallback():
    row = _row("GOPR1234.MP4")
    assert file_camera(row) == "GoPro"


def test_file_camera_none_when_no_signal():
    row = _row("random.mp4")
    assert file_camera(row) is None


# --- tags_for_file ------------------------------------------------------------

_TAGS = [
    "Events/2026-06-corsica-sardinia-yacht",
    "Gear/Camera/DJI FC8282",
    "Gear/Camera/Insta360",
    "Source/DJI",
]


def test_tags_for_file_matches_gear_camera_substring():
    # DJI's actual EXIF model (FC8282) should match the notes' gear tag.
    per_file = tags_for_file("DJI FC8282", _TAGS)
    assert "Gear/Camera/DJI FC8282" in per_file
    assert "Gear/Camera/Insta360" not in per_file
    assert "Events/2026-06-corsica-sardinia-yacht" in per_file
    assert "Source/DJI" in per_file


def test_tags_for_file_synthesizes_gear_tag_when_unmatched():
    per_file = tags_for_file("Canon EOS R7", _TAGS)
    assert "Gear/Camera/Canon EOS R7" in per_file
    assert "Gear/Camera/DJI FC8282" not in per_file
    assert "Gear/Camera/Insta360" not in per_file


def test_tags_for_file_base_tags_only_when_no_camera():
    per_file = tags_for_file(None, _TAGS)
    assert per_file == ["Events/2026-06-corsica-sardinia-yacht", "Source/DJI"]


def test_tags_for_file_empty_tags_list_still_synthesizes_gear_tag():
    # Callers (`_propose`, `tagsync`) guard `if not tags: return []` before
    # calling; this only documents tags_for_file's own behavior in isolation.
    assert tags_for_file("DJI FC8282", []) == ["Gear/Camera/DJI FC8282"]


def test_tags_for_file_empty_tags_no_camera():
    assert tags_for_file(None, []) == []
