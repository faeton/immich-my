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

Drift is found by offset search, never by comparing medians (that told
a drone flown only on day 9 of a 10-day trip it was "+83h"). Each
camera's bursts are collapsed into independent events (shots within
`EVENT_GAP_SECONDS`). Candidate offsets are whole hours (±`MAX_HOURS`)
plus a small skew (±`SKEW_SECONDS`, step `SKEW_STEP_SECONDS`); each is
scored by one-to-one event matches within `MATCH_TOLERANCE_SECONDS`
(each reference event used at most once). Candidates sharing an hour
form one peak. The best peak is accepted only when it has ≥
`MIN_MATCHED_EVENTS` matches covering ≥ `MIN_COVERAGE` of the smaller
camera's events in the overlapping window, and dominates the runner-up
peak (the zero-offset peak included) by ≥ `DOMINANCE_MARGIN` and ≥
`DOMINANCE_RATIO`×. The offset is refined by the median matched delta;
under `MIN_DRIFT_SECONDS` it's no drift. Anything else is ambiguous → no
proposal.

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

from bisect import bisect_left
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
EVENT_GAP_SECONDS = 60             # shots this close are one burst = one event
MAX_HOURS = 14                     # whole-hour offsets searched: -14h..+14h
SKEW_SECONDS = 5 * 60              # ...each plus a skew of up to ±5 min
SKEW_STEP_SECONDS = 30
MATCH_TOLERANCE_SECONDS = 2 * 60   # shifted events this close to a ref event match
MIN_MATCHED_EVENTS = 3             # independent matched events needed
MIN_COVERAGE = 0.3                 # of the smaller camera's events in the overlap
DOMINANCE_MARGIN = 2               # best peak ≥ runner-up + 2 ...
DOMINANCE_RATIO = 2                # ... and ≥ 2 × runner-up
# Cap for deltas not backed by evidence from another clock (single-camera
# `clock-drift` guesses); such guesses are never MEDIUM/HIGH.
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


def _events(times: list[datetime]) -> list[float]:
    """Collapse bursts (consecutive shots ≤ EVENT_GAP_SECONDS apart) into
    independent events; returns each event's first-shot timestamp, sorted."""
    out: list[float] = []
    last = None
    for ts in sorted(t.timestamp() for t in times):
        if last is None or ts - last > EVENT_GAP_SECONDS:
            out.append(ts)
        last = ts
    return out


def _match(cam: list[float], ref: list[float], offset: float) -> list[float]:
    """One-to-one matches of `cam` events shifted by `offset` onto `ref`
    events within MATCH_TOLERANCE_SECONDS (each ref event used once,
    nearest free one wins). Returns each match's residual (ref - shifted)."""
    used: set[int] = set()
    residuals: list[float] = []
    for c in cam:
        x = c + offset
        i = bisect_left(ref, x - MATCH_TOLERANCE_SECONDS)
        best = None
        while i < len(ref) and ref[i] <= x + MATCH_TOLERANCE_SECONDS:
            if i not in used and (best is None or abs(ref[i] - x) < abs(ref[best] - x)):
                best = i
            i += 1
        if best is not None:
            used.add(best)
            residuals.append(ref[best] - x)
    return residuals


def _coverage_base(cam: list[float], ref: list[float], offset: float) -> int:
    """Event count of the smaller camera inside the window where both
    cameras (camera shifted by `offset`) have events."""
    lo = max(cam[0] + offset, ref[0]) - MATCH_TOLERANCE_SECONDS
    hi = min(cam[-1] + offset, ref[-1]) + MATCH_TOLERANCE_SECONDS
    n_cam = sum(1 for c in cam if lo <= c + offset <= hi)
    n_ref = sum(1 for r in ref if lo <= r <= hi)
    return min(n_cam, n_ref)


def _estimate_drift(
    cam_times: list[datetime], ref_times: list[datetime],
) -> tuple[float, int] | None:
    """(delta seconds to add to the camera, matched event count), or None
    when there's no drift or no unambiguous offset."""
    cam, ref = _events(cam_times), _events(ref_times)
    skews = range(-SKEW_SECONDS, SKEW_SECONDS + 1, SKEW_STEP_SECONDS)
    # Peak per whole hour: its best-scoring candidate offset.
    peaks: dict[int, tuple[int, float]] = {}
    for h in range(-MAX_HOURS, MAX_HOURS + 1):
        for eps in skews:
            offset = h * 3600.0 + eps
            score = len(_match(cam, ref, offset))
            if h not in peaks or score > peaks[h][0]:
                peaks[h] = (score, offset)
    ranked = sorted(peaks.values(), key=lambda p: p[0], reverse=True)
    (best, offset), runner_up = ranked[0], ranked[1][0]
    if best < MIN_MATCHED_EVENTS:
        return None
    if best < runner_up + DOMINANCE_MARGIN or best < DOMINANCE_RATIO * runner_up:
        return None
    base = _coverage_base(cam, ref, offset)
    if base == 0 or best < MIN_COVERAGE * base:
        return None
    return offset + median(_match(cam, ref, offset)), best


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
            f"{ref_cam} ({len(ref_items)} ref files; {pairs} independent "
            f"events match); proposed: add "
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
