"""Cross-camera clock drift — the case `clock-drift` (folder-median)
can't handle on its own.

File-outlier drift: one shot in 200 is days off → `clock-drift` fixes it
by snapping the outlier to the folder median. Works because the folder
is *overwhelmingly* one clock.

Group drift: you shoot a trip with two bodies and one was never clock-
synced (or lost its RTC battery, or was set wrong on purpose for video
sync, or drifted over weeks). 80/200 files are now 3 h behind the other
120. Folder-median flags the minority as outliers but proposes the
wrong fix — snapping 80 files to a single median datetime collapses
every shot from that camera onto one instant.

This rule groups files by camera `(Make, Model)`, picks a reference
group (the camera with the most GPS-tagged files, since those are
satellite-synced; tie-break on group size), and proposes a per-camera
*delta*. Each off-camera file gets `DateTimeOriginal = original + delta`,
preserving the intra-camera sequence.

Sanity thresholds are deliberately conservative — cameras usually stay
synced via GPS, phone sync, or manual set, so a real drift is either
tens of minutes (time-zone mixup) or hours (manual slip). Noise below
5 min isn't worth a prompt; drift above 14 days is probably not drift
but "this file is from a different trip".

MEDIUM, because we're proposing a bulk rewrite of capture time — the
user should look at the delta before accepting. All findings in one
camera share a `group` key so the MEDIUM prompter asks once per camera,
not once per file.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

from ..dates import resolve as resolve_date
from ..exif import ExifRow, has_gps
from .registry import Finding, Rule, register


MIN_GROUP = 3                      # need enough samples for a stable median
MIN_CAMERAS = 2                    # rule only makes sense with ≥2 groups
MIN_DRIFT_SECONDS = 5 * 60         # below this is sync noise
MAX_DRIFT_SECONDS = 14 * 86400     # above this: different trip / date typo


def camera_key(row: ExifRow) -> str | None:
    make = (row.get("EXIF:Make", "QuickTime:Make") or "").strip()
    model = (row.get("EXIF:Model", "QuickTime:Model") or "").strip()
    if not (make or model):
        return None
    return f"{make} {model}".strip()


def _fmt_delta(seconds: float) -> str:
    sign = "+" if seconds >= 0 else "-"
    n = int(abs(seconds))
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{h}h{m:02d}m"
    if m:
        return f"{sign}{m}m{s:02d}s"
    return f"{sign}{s}s"


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    by_cam: dict[str, list[tuple[ExifRow, datetime]]] = defaultdict(list)
    for r in rows:
        authority = resolve_date(r)
        if authority is None or authority.source == "mtime":
            continue
        cam = camera_key(r)
        if cam is None:
            continue
        by_cam[cam].append((r, authority.dt))

    groups = {cam: items for cam, items in by_cam.items() if len(items) >= MIN_GROUP}
    if len(groups) < MIN_CAMERAS:
        return []

    def gps_count(items: list[tuple[ExifRow, datetime]]) -> int:
        return sum(1 for r, _ in items if has_gps(r))

    ref_cam = max(groups, key=lambda c: (gps_count(groups[c]), len(groups[c])))
    ref_items = groups[ref_cam]
    ref_median = median(dt.timestamp() for _, dt in ref_items)

    out: list[Finding] = []
    for cam, items in groups.items():
        if cam == ref_cam:
            continue
        cam_median = median(dt.timestamp() for _, dt in items)
        delta = ref_median - cam_median
        if abs(delta) < MIN_DRIFT_SECONDS or abs(delta) > MAX_DRIFT_SECONDS:
            continue
        group_id = f"clock-drift-camera:{cam}"
        reason = (
            f"{cam} ({len(items)} files) is {_fmt_delta(-delta)} vs "
            f"{ref_cam} ({len(ref_items)} ref files); proposed: add "
            f"{_fmt_delta(delta)} to each DateTimeOriginal"
        )
        for row, dt in items:
            new_dt = datetime.fromtimestamp(dt.timestamp() + delta)
            out.append(Finding(
                rule="clock-drift-by-camera",
                confidence="medium",
                path=row.path,
                action="write_xmp",
                patch={"DateTimeOriginal": new_dt.strftime("%Y:%m:%d %H:%M:%S")},
                reason=reason,
                group=group_id,
            ))
    return out


register(Rule(name="clock-drift-by-camera", confidence="medium", propose=_propose))
