from __future__ import annotations

from pathlib import Path

from immy.exif import ExifRow
from immy.rules.makernote_present import _propose


def _row(path: Path, **raw) -> ExifRow:
    return ExifRow(path=path, raw=raw)


def test_flags_file_with_makernote(tmp_path: Path):
    row = _row(
        tmp_path / "IMG_0001.JPG",
        **{"MakerNotes:SerialNumber": "1234567", "EXIF:Make": "NIKON CORPORATION"},
    )
    findings = _propose([row], tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "makernote-present"
    assert findings[0].action == "note"
    assert findings[0].confidence == "low"
    assert "MakerNote" in findings[0].reason


def test_silent_without_makernote(tmp_path: Path):
    row = _row(tmp_path / "IMG_0002.JPG", **{"EXIF:Make": "Apple"})
    assert _propose([row], tmp_path) == []


def test_one_finding_per_file(tmp_path: Path):
    rows = [
        _row(tmp_path / "a.jpg", **{"MakerNotes:FocusMode": "AF-C"}),
        _row(tmp_path / "b.jpg", **{"MakerNotes:ShutterCount": 42_000}),
        _row(tmp_path / "c.jpg"),
    ]
    findings = _propose(rows, tmp_path)
    assert len(findings) == 2
    assert {f.path.name for f in findings} == {"a.jpg", "b.jpg"}
