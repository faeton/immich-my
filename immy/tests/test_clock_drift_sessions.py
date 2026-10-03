from __future__ import annotations

from pathlib import Path

from immy.exif import ExifRow
from immy.rules.clock_drift import _propose


def _row(name: str, dt: str) -> ExifRow:
    return ExifRow(path=Path(name), raw={"EXIF:DateTimeOriginal": dt})


def test_second_shooting_day_is_not_drift():
    # 2026-08-la-manga shape: 4 clips on day 1, 3 clips four days later.
    # The later day is a real session — snapping it to the median would
    # collapse it onto one instant of day 1.
    rows = [_row(f"A{i}.MP4", f"2026:08:31 10:{40 + i}:00") for i in range(4)]
    rows += [_row(f"B{i}.MP4", f"2026:09:04 10:{54 + i}:00") for i in range(3)]
    assert _propose(rows, Path(".")) == []


def test_lone_straggler_on_another_day_still_flagged():
    rows = [_row(f"A{i}.MP4", f"2026:08:31 10:{40 + i}:00") for i in range(4)]
    rows += [_row(f"B{i}.MP4", f"2026:09:04 10:{54 + i}:00") for i in range(3)]
    rows.append(_row("C.MP4", "2027:01:01 00:00:00"))
    findings = _propose(rows, Path("."))
    assert [f.path.name for f in findings] == ["C.MP4"]
