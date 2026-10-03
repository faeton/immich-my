"""Single-clock outlier detector.

When a camera's clock is set wrong for a shot or two (a body that lost
its RTC battery, a year never set after a reset), the file sits days to
years away from its siblings. Rather than trust EXIF blindly, we look at
the folder's own timeline.

Capture dates use the authoritative date per file (EXIF > companion SRT
> filename — mtime is excluded, it's too noisy). Files are split into
sessions (no gap > `SESSION_GAP_SECONDS`). A file is an outlier only
when its session is more than `ISOLATION_SECONDS` from every other
session — isolated in both directions — it is not the folder's biggest
session, and outliers are a small minority of the folder (a sparse
trip with one shot a day is not ten clock errors).
Distance from the folder median is never used: a 10-day trip is >24 h
from its own median on 8 days out of 10.

The proposal is a *delta*, never a constant: when shifting the file by a
whole number of years (same time of day, joining a session) or by a
whole number of hours (landing inside a session) gives exactly one
candidate, the patch is `original + delta` (hour shifts capped at
`MAX_UNCORROBORATED_SECONDS`). A single clock can't corroborate its own
shift — a unique landing offset is still a guess (a genuine next-day
shot 25 h later "lands" too) — so every finding is LOW: shown in the
report, never auto-applied by `--yes-medium`. With no clean candidate
the finding is a LOW note with no patch.

Multi-camera folders are left to `clock-drift-by-camera`.

Runs late so it sees dates written by earlier rules (dji-date-from-srt
etc.) via the two-pass apply.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import ceil, floor
from pathlib import Path

from ..dates import DateAuthority, resolve as resolve_date
from ..exif import ExifRow
from .clock_drift_by_camera import (
    MAX_UNCORROBORATED_SECONDS,
    MIN_GROUP,
    SESSION_GAP_SECONDS,
    _fmt_delta,
    camera_key,
    split_sessions,
)
from .registry import Finding, Rule, register


ISOLATION_SECONDS = 24 * 3600
MIN_SAMPLES = 3
MAX_OUTLIER_FRACTION = 0.25
REAL_SESSION_FILES = 3


def _multi_camera_folder(rows: list[ExifRow]) -> bool:
    """True when ≥2 camera groups are each big enough to have their own
    sessions. Hands off to `clock-drift-by-camera` in that case."""
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        cam = camera_key(r)
        if cam is not None:
            counts[cam] += 1
    return sum(1 for n in counts.values() if n >= MIN_GROUP) >= 2


def _isolated_sessions(
    sessions: list[list[datetime]],
) -> list[int]:
    """Indices of sessions > ISOLATION_SECONDS from both the previous and
    the next session (a missing neighbour counts as isolated)."""
    out = []
    for i, s in enumerate(sessions):
        before = (s[0] - sessions[i - 1][-1]).total_seconds() if i > 0 else float("inf")
        after = (sessions[i + 1][0] - s[-1]).total_seconds() if i + 1 < len(sessions) else float("inf")
        if before > ISOLATION_SECONDS and after > ISOLATION_SECONDS:
            out.append(i)
    return out


def _snap_deltas(dt: datetime, sessions: list[list[datetime]]) -> set[float]:
    """Candidate deltas (seconds) that move `dt` into a session: whole years
    (time of day kept, landing within SESSION_GAP_SECONDS of the session)
    or whole hours up to MAX_UNCORROBORATED_SECONDS (landing inside it)."""
    gap = timedelta(seconds=SESSION_GAP_SECONDS)
    out: set[float] = set()
    for s in sessions:
        start, end = s[0], s[-1]
        for year in {start.year, end.year} - {dt.year}:
            try:
                shifted = dt.replace(year=year)
            except ValueError:  # Feb 29 into a non-leap year
                continue
            if start - gap <= shifted <= end + gap:
                out.add((shifted - dt).total_seconds())
        lo = ceil((start - dt).total_seconds() / 3600)
        hi = floor((end - dt).total_seconds() / 3600)
        max_h = MAX_UNCORROBORATED_SECONDS // 3600
        for h in range(max(lo, -max_h), min(hi, max_h) + 1):
            if h:
                out.add(h * 3600.0)
    return out


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    if _multi_camera_folder(rows):
        return []
    authorities = [(r, resolve_date(r)) for r in rows]
    authorities = [(r, a) for r, a in authorities if a is not None and a.source != "mtime"]
    if len(authorities) < MIN_SAMPLES:
        return []
    by_dt: dict[datetime, list[tuple[ExifRow, DateAuthority]]] = defaultdict(list)
    for r, a in authorities:
        by_dt[a.dt].append((r, a))
    all_sessions = split_sessions(list(by_dt))
    # The folder's biggest session is the body of the trip, never an outlier
    # (in a two-session folder both sessions are "isolated" from each other).
    # A session of REAL_SESSION_FILES or more is a real shooting day (a
    # second flight four days later), not a stray clock — leave it alone.
    sizes = [sum(len(by_dt[t]) for t in sess) for sess in all_sessions]
    body = max(range(len(all_sessions)), key=sizes.__getitem__)
    isolated = {
        i for i in _isolated_sessions(all_sessions)
        if i != body and sizes[i] < REAL_SESSION_FILES
    }
    n_outliers = sum(sizes[i] for i in isolated)
    if not isolated or n_outliers > MAX_OUTLIER_FRACTION * len(authorities):
        return []
    sessions = [s for i, s in enumerate(all_sessions) if i not in isolated]
    outliers = [ra for i in sorted(isolated) for t in all_sessions[i] for ra in by_dt[t]]

    out: list[Finding] = []
    for row, authority in outliers:
        nearest = min(
            min(abs((s[0] - authority.dt).total_seconds()),
                abs((s[-1] - authority.dt).total_seconds()))
            for s in sessions
        )
        this_str = authority.dt.strftime("%Y-%m-%d %H:%M:%S")
        base = (
            f"{nearest / 86400:.1f}d from the folder's other sessions "
            f"(source={authority.source}, this={this_str})"
        )
        candidates = _snap_deltas(authority.dt, sessions)
        if len(candidates) != 1:
            out.append(Finding(
                rule="clock-drift",
                confidence="low",
                path=row.path,
                action="note",
                reason=f"{base}; no clean whole-year/hour offset — check by hand",
            ))
            continue
        delta = candidates.pop()
        new_dt = authority.dt + timedelta(seconds=delta)
        out.append(Finding(
            rule="clock-drift",
            confidence="low",
            path=row.path,
            action="write_xmp",
            patch={"DateTimeOriginal": new_dt.strftime("%Y:%m:%d %H:%M:%S")},
            reason=f"{base}; proposed: add {_fmt_delta(delta)} → {new_dt.strftime('%Y-%m-%d %H:%M:%S')}",
        ))
    return out


register(Rule(name="clock-drift", confidence="low", propose=_propose))
