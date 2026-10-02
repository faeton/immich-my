"""Cross-camera clock drift — the case the single-clock `clock-drift`
rule can't handle on its own.

File-outlier drift: one shot in 200 is a year off → `clock-drift` flags
the isolated file. Works because the folder is one clock.

Group drift: you shoot a trip with two bodies and one was never clock-
synced (or lost its RTC battery, or was set wrong on purpose for video
sync, or drifted over weeks). 80/200 files are now 40 min behind the
other 120. The fix has to be a per-camera delta — snapping 80 files to
one datetime would collapse every shot from that camera onto one instant.

This rule groups files by camera `(Make, Model)`, picks a reference
group (the camera with the most GPS-tagged files, since those are
satellite-synced; tie-break on group size), and proposes a per-camera
*delta*. Each off-camera file gets `DateTimeOriginal = original + delta`,
preserving the intra-camera sequence.

Drift is only inferred from temporally overlapping evidence. Each
camera's files are split into sessions (runs with no gap >
`SESSION_GAP_SECONDS`); a camera session is compared only with reference
sessions it overlaps in *raw* time (± `OVERLAP_TOLERANCE_SECONDS`). For
every file in such a session we take the delta to its nearest reference
neighbour; the median of those deltas is the drift estimate, and it
needs ≥ `MIN_PAIRS` pairs that agree with it (within
`AGREE_TOLERANCE_SECONDS`) to count. No overlap → no proposal: comparing
per-camera medians told a drone flown only on day 9 of a 10-day trip
that it was "+83h" off.

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
from ..exif import ExifRow, has_valid_gps as has_gps
from .registry import Finding, Rule, register


MIN_GROUP = 3                      # need enough samples for a stable median
MIN_CAMERAS = 2                    # rule only makes sense with ≥2 groups
MIN_DRIFT_SECONDS = 5 * 60         # below this is sync noise
MAX_DRIFT_SECONDS = 14 * 86400     # above this: different trip / date typo
SESSION_GAP_SECONDS = 3 * 3600     # a gap longer than this starts a new session
OVERLAP_TOLERANCE_SECONDS = 3600   # raw-time slack when testing session overlap
MIN_PAIRS = 3                      # nearest-neighbour pairs needed to infer drift
AGREE_TOLERANCE_SECONDS = 5 * 60   # a pair "agrees" when within this of the median
# A delta not backed by overlapping evidence from another clock is a guess;
# above this it never gets MEDIUM/HIGH (so `--yes-medium` can't apply it).
MAX_UNCORROBORATED_SECONDS = 26 * 3600


def camera_key(row: ExifRow) -> str | None:
    make = (row.get("EXIF:Make", "QuickTime:Make") or "").strip()
    model = (row.get("EXIF:Model", "QuickTime:Model") or "").strip()
    if not (make or model):
        return None
    return f"{make} {model}".strip()


def split_sessions(times: list[datetime]) -> list[list[datetime]]:
    """Group capture times into sessions: maximal runs, in time order, with
    no gap > `SESSION_GAP_SECONDS` between consecutive shots."""
    sessions: list[list[datetime]] = []
    for t in sorted(times):
        if sessions and (t - sessions[-1][-1]).total_seconds() <= SESSION_GAP_SECONDS:
            sessions[-1].append(t)
        else:
            sessions.append([t])
    return sessions


def _overlaps(a: list[datetime], b: list[datetime]) -> bool:
    slack = OVERLAP_TOLERANCE_SECONDS
    return (
        a[0].timestamp() <= b[-1].timestamp() + slack
        and b[0].timestamp() <= a[-1].timestamp() + slack
    )


def _estimate_drift(
    cam_times: list[datetime], ref_times: list[datetime],
) -> tuple[float, int] | None:
    """(delta seconds to add to the camera, agreeing pair count), or None
    when the camera's sessions don't overlap the reference's in enough
    places, or the nearest-neighbour deltas don't agree on one offset."""
    ref_sessions = split_sessions(ref_times)
    deltas: list[float] = []
    for session in split_sessions(cam_times):
        ref_near = [t.timestamp() for rs in ref_sessions if _overlaps(session, rs) for t in rs]
        if not ref_near:
            continue
        for t in session:
            ts = t.timestamp()
            deltas.append(min(ref_near, key=lambda r: abs(r - ts)) - ts)
    if len(deltas) < MIN_PAIRS:
        return None
    delta = median(deltas)
    agreeing = sum(1 for d in deltas if abs(d - delta) <= AGREE_TOLERANCE_SECONDS)
    if agreeing < MIN_PAIRS or agreeing * 2 < len(deltas):
        return None
    return delta, agreeing


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
    ref_times = [dt for _, dt in ref_items]

    out: list[Finding] = []
    for cam, items in groups.items():
        if cam == ref_cam:
            continue
        estimate = _estimate_drift([dt for _, dt in items], ref_times)
        if estimate is None:
            continue
        delta, pairs = estimate
        if abs(delta) < MIN_DRIFT_SECONDS or abs(delta) > MAX_DRIFT_SECONDS:
            continue
        group_id = f"clock-drift-camera:{cam}"
        reason = (
            f"{cam} ({len(items)} files) is {_fmt_delta(-delta)} vs "
            f"{ref_cam} ({len(ref_items)} ref files; {pairs} overlapping "
            f"shot pairs agree); proposed: add "
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
