"""Unit tests for the clock-drift rules (`clock-drift`, `clock-drift-by-camera`).

Regression coverage for the 2026-10 audit: the folder rule used to flag
every file >24 h from the folder median and patch them all to that one
median instant (a 10-day single-phone trip had 8/10 files collapsed by
`--yes-medium`), and the cross-camera rule compared per-camera medians
(a drone flown only on day 9 was told "+83h").
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from immy.exif import ExifRow
from immy.rules import clock_drift, clock_drift_by_camera as by_camera

FOLDER = Path("/trip")
PHONE = ("Apple", "iPhone 15 Pro")
DRONE = ("DJI", "Mini 4 Pro")
SONY = ("SONY", "ILCE-7M4")


def _row(name: str, dt: datetime, cam=PHONE, gps: bool = False) -> ExifRow:
    raw = {
        "EXIF:DateTimeOriginal": dt.strftime("%Y:%m:%d %H:%M:%S"),
        "EXIF:Make": cam[0],
        "EXIF:Model": cam[1],
    }
    if gps:
        raw["EXIF:GPSLatitude"] = 45.5
        raw["EXIF:GPSLongitude"] = 7.25
    return ExifRow(FOLDER / name, raw)


def _patched(f) -> datetime:
    return datetime.strptime(f.patch["DateTimeOriginal"], "%Y:%m:%d %H:%M:%S")


# ---------------------------------------------------------------- clock-drift


def test_single_camera_ten_day_trip_is_not_flagged():
    # Repro: one phone, one shot per day for 10 days. Old rule flagged 8/10
    # (>24h from the median) and patched every one to the same median instant.
    rows = [_row(f"p{d}.jpg", datetime(2025, 7, d, 12)) for d in range(1, 11)]
    assert clock_drift._propose(rows, FOLDER) == []


def test_single_camera_sparse_trip_with_gaps_over_24h_is_not_flagged():
    # One shot per day at drifting times: every gap is 25 h, so every file is
    # "isolated" — that's a sparse trip, not ten clock errors.
    start = datetime(2025, 7, 1, 9)
    rows = [_row(f"p{d}.jpg", start + timedelta(hours=25 * d)) for d in range(10)]
    assert clock_drift._propose(rows, FOLDER) == []


def test_single_camera_continuous_multi_day_trip_is_not_flagged():
    rows = []
    for d in range(1, 8):
        for h in (9, 11, 14, 17):
            rows.append(_row(f"p{d}_{h}.jpg", datetime(2025, 7, d, h)))
    assert clock_drift._propose(rows, FOLDER) == []


def _session(day: int = 1) -> list[ExifRow]:
    base = datetime(2026, 4, day, 10)
    return [_row(f"s{day}_{i}.jpg", base + timedelta(minutes=5 * i)) for i in range(6)]


def test_single_camera_year_off_outlier_gets_year_delta_low_confidence():
    rows = _session() + [_row("odd.jpg", datetime(2025, 4, 1, 10, 7))]
    findings = clock_drift._propose(rows, FOLDER)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "clock-drift"
    assert f.path == FOLDER / "odd.jpg"
    # Whole-year delta keeps the time of day; uncorroborated >26h → LOW,
    # so `--yes-medium` never auto-applies it.
    assert f.confidence == "low"
    assert f.action == "write_xmp"
    assert _patched(f) == datetime(2026, 4, 1, 10, 7)


def test_single_camera_next_day_shot_is_never_medium():
    # Six photos Apr 1 10:00-10:25 + one genuine Apr 2 11:05 photo. Shifting
    # -25 h "lands" it at 10:05, but a unique landing offset from one clock is
    # not corroboration: LOW (shown, never auto-applied by --yes-medium).
    rows = _session() + [_row("odd.jpg", datetime(2026, 4, 2, 11, 5))]
    findings = clock_drift._propose(rows, FOLDER)
    assert len(findings) == 1
    f = findings[0]
    assert f.confidence == "low"
    assert f.action == "write_xmp"
    assert _patched(f) == datetime(2026, 4, 1, 10, 5)


def test_single_camera_outlier_without_clean_delta_gets_no_patch():
    # 4 days later at an unrelated time: report it, but no auto-applicable fix
    # (and certainly not the folder median).
    rows = _session() + [_row("odd.jpg", datetime(2026, 4, 5, 15, 33))]
    findings = clock_drift._propose(rows, FOLDER)
    assert len(findings) == 1
    f = findings[0]
    assert f.confidence == "low"
    assert f.action == "note"
    assert f.patch == {}


def test_single_camera_outliers_keep_their_own_deltas_never_a_constant():
    rows = _session() + [
        _row("odd1.jpg", datetime(2025, 4, 1, 10, 3)),
        _row("odd2.jpg", datetime(2025, 4, 1, 10, 21)),
    ]
    findings = {f.path.name: f for f in clock_drift._propose(rows, FOLDER)}
    assert _patched(findings["odd1.jpg"]) == datetime(2026, 4, 1, 10, 3)
    assert _patched(findings["odd2.jpg"]) == datetime(2026, 4, 1, 10, 21)


def test_single_camera_hands_off_multi_camera_folders():
    rows = _session() + [_row(f"d{i}.mp4", datetime(2027, 1, 1, i), cam=DRONE) for i in range(3)]
    assert clock_drift._propose(rows, FOLDER) == []


# ------------------------------------------------------ clock-drift-by-camera


def _phone_ten_days() -> list[ExifRow]:
    return [_row(f"p{d}.jpg", datetime(2025, 7, d, 12), gps=True) for d in range(1, 11)]


def test_by_camera_drone_flown_one_day_gets_no_median_delta():
    # Repro: phone shoots days 1-10, drone only on day 9 around the phone's
    # day-9 shot. Old rule compared medians and proposed "+83h".
    rows = _phone_ten_days() + [
        _row(f"d{i}.mp4", datetime(2025, 7, 9, 10 + i), cam=DRONE) for i in range(3)
    ]
    assert by_camera._propose(rows, FOLDER) == []


def test_by_camera_correctly_timed_burst_is_not_drift():
    # Phone noon shots days 1-10 + three correctly timed drone shots on day 9
    # at 11:40:00-02: one burst = one event, one reference shot — not three
    # supporting pairs, and certainly not a bulk correction.
    rows = _phone_ten_days() + [
        _row(f"d{i}.mp4", datetime(2025, 7, 9, 11, 40, i), cam=DRONE) for i in range(3)
    ]
    assert by_camera._propose(rows, FOLDER) == []


def _trip_events() -> list[datetime]:
    """Irregular shooting moments over three days (fixed, not periodic)."""
    minutes = [0, 23, 71, 118, 160, 247, 301, 389, 452, 530, 611, 655]
    return [
        datetime(2026, 4, day, 8) + timedelta(minutes=m + 7 * day)
        for day in (1, 2, 3) for m in minutes
    ]


def _slipped(offset: timedelta, cam=SONY, prefix="b") -> list[ExifRow]:
    """The camera shoots every trip event (a 2-shot burst each) with its
    clock reading `offset` ahead."""
    rows = []
    for i, t in enumerate(_trip_events()):
        for k in range(2):
            rows.append(_row(f"{prefix}{i}_{k}.jpg", t + offset + timedelta(seconds=k), cam=cam))
    return rows


def _phone_trip() -> list[ExifRow]:
    return [_row(f"a{i}.jpg", t, gps=True) for i, t in enumerate(_trip_events())]


def test_by_camera_timezone_slip_plus_3h_is_found():
    rows = _phone_trip() + _slipped(timedelta(hours=3))
    findings = by_camera._propose(rows, FOLDER)
    assert len(findings) == 2 * len(_trip_events())
    assert all(f.rule == "clock-drift-by-camera" and f.confidence == "medium" for f in findings)
    assert len({f.group for f in findings}) == 1
    # Each file shifted by -3h00m: spacing preserved, never a constant.
    by_name = {f.path.name: f for f in findings}
    for i, t in enumerate(_trip_events()):
        assert _patched(by_name[f"b{i}_0.jpg"]) == t
        assert _patched(by_name[f"b{i}_1.jpg"]) == t + timedelta(seconds=1)


def test_by_camera_finds_each_slipped_camera_independently():
    rows = (_phone_trip() + _slipped(timedelta(hours=3))
            + _slipped(-timedelta(hours=7, minutes=2), cam=DRONE, prefix="d"))
    by_name = {f.path.name: f for f in by_camera._propose(rows, FOLDER)}
    for i, t in enumerate(_trip_events()):
        assert _patched(by_name[f"b{i}_0.jpg"]) == t
        assert _patched(by_name[f"d{i}_0.jpg"]) == t


def test_by_camera_synced_cameras_get_no_proposal():
    assert by_camera._propose(_phone_trip() + _slipped(timedelta(0)), FOLDER) == []
    # Sub-5-minute skew is sync noise.
    assert by_camera._propose(_phone_trip() + _slipped(timedelta(minutes=2)), FOLDER) == []


def test_by_camera_periodic_shooting_is_ambiguous():
    # Hourly shots on both bodies: +4h/+5h/+6h all line up nearly as well,
    # no peak dominates → no proposal.
    rows = [_row(f"a{i}.jpg", datetime(2026, 4, 1, 10 + i), gps=True) for i in range(4)]
    rows += [_row(f"b{i}.jpg", datetime(2026, 4, 1, 5 + i), cam=SONY) for i in range(4)]
    assert by_camera._propose(rows, FOLDER) == []


def test_by_camera_needs_three_matched_events():
    rows = _phone_trip() + [
        r for r in _slipped(timedelta(hours=3)) if r.path.name.split("_")[0] in ("b0", "b5")
    ]
    assert by_camera._propose(rows, FOLDER) == []


def test_by_camera_inconsistent_deltas_are_not_evidence():
    # Overlapping sessions but the deltas disagree (cameras simply shot at
    # different moments) — no consistent offset, no proposal.
    rows = [_row(f"a{i}.jpg", datetime(2026, 4, 1, 10 + 2 * i), gps=True) for i in range(4)]
    rows += [
        _row(f"b{i}.jpg", datetime(2026, 4, 1, 10 + 2 * i, m), cam=SONY)
        for i, m in enumerate((10, 35, 50, 20))
    ]
    assert by_camera._propose(rows, FOLDER) == []


def test_split_sessions_breaks_on_gaps_over_three_hours():
    t0 = datetime(2026, 4, 1, 8)
    times = [t0, t0 + timedelta(hours=3), t0 + timedelta(hours=6, seconds=1)]
    sessions = by_camera.split_sessions(times)
    assert sessions == [[times[0], times[1]], [times[2]]]


def test_by_camera_coverage_counts_all_events_of_smaller_camera():
    # 103 events per camera; only three line up (at +3h), every other event
    # is days away from anything on the other body. 3/103 is a coincidence,
    # not a clock offset — no MEDIUM bulk rewrite of the whole camera.
    rows = []
    for i, m in enumerate((0, 41, 97)):
        t = datetime(2026, 4, 1, 9) + timedelta(minutes=m)
        rows.append(_row(f"a{i}.jpg", t, gps=True))
        rows.append(_row(f"b{i}.jpg", t + timedelta(hours=3), cam=SONY))
    for i in range(100):
        rows.append(_row(f"ax{i}.jpg", datetime(2026, 4, 5, 8) + timedelta(minutes=37 * i), gps=True))
        rows.append(_row(f"bx{i}.jpg", datetime(2026, 4, 15, 8) + timedelta(minutes=37 * i), cam=SONY))
    findings = by_camera._propose(rows, FOLDER)
    assert not [f for f in findings if f.confidence in ("medium", "high")]
