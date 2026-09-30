"""Folder-coherence clock-drift detector.

When one camera in a folder has its clock set wrong (a common artefact
of a camera body that lost its RTC battery, or a body that never had
its date set after a reset), the file sits days-to-years away from its
siblings. Rather than trust EXIF blindly, we look at the folder's
collective opinion.

Compute the median capture datetime across all files (using the
authoritative date per file: EXIF > companion SRT > filename — mtime
is excluded, it's too noisy). Any file >24 h from the median is
flagged MEDIUM with its source and the delta. Proposed patch is the
median datetime itself — good enough for single-file outliers, which
is the common case. Group drift (whole camera off by N hours) will
want a richer propose/accept UX in a later iteration.

Multi-day trips: timestamps are split into sessions wherever the gap
between consecutive files exceeds the threshold. A session of
≥ MIN_SAMPLES files is a real shooting day, not drift — snapping it to
the median would collapse the whole day onto one instant (2026-08-la-manga:
12 Sep-4 drone clips proposed as 2026-08-31 10:50:13). Only files in
smaller sessions are candidates.

Runs late so it sees dates written by earlier rules (dji-date-from-srt
etc.) via the two-pass apply.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

from collections import defaultdict

from ..dates import resolve as resolve_date
from ..exif import ExifRow
from .clock_drift_by_camera import MIN_GROUP, camera_key
from .registry import Finding, Rule, register


DRIFT_THRESHOLD_SECONDS = 24 * 3600
MIN_SAMPLES = 3


def _multi_camera_folder(rows: list[ExifRow]) -> bool:
    """True when ≥2 camera groups are each big enough to have their own
    median. Hands off to `clock-drift-by-camera` in that case — snapping
    a whole camera's worth of files to the folder median would collapse
    them to one instant, which is wrong."""
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        cam = camera_key(r)
        if cam is not None:
            counts[cam] += 1
    return sum(1 for n in counts.values() if n >= MIN_GROUP) >= 2


def _small_session_paths(authorities: list) -> set[Path]:
    """Paths of files in sessions (runs split on >threshold gaps) with
    fewer than MIN_SAMPLES members — the only plausible single-file drift."""
    ordered = sorted(authorities, key=lambda ra: ra[1].dt.timestamp())
    sessions: list[list[Path]] = []
    prev_ts: float | None = None
    for row, authority in ordered:
        ts = authority.dt.timestamp()
        if prev_ts is None or ts - prev_ts > DRIFT_THRESHOLD_SECONDS:
            sessions.append([])
        sessions[-1].append(row.path)
        prev_ts = ts
    return {p for s in sessions if len(s) < MIN_SAMPLES for p in s}


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    if _multi_camera_folder(rows):
        return []
    authorities = [(r, resolve_date(r)) for r in rows]
    authorities = [(r, a) for r, a in authorities if a is not None and a.source != "mtime"]
    if len(authorities) < MIN_SAMPLES:
        return []
    ts_vals = [a.dt.timestamp() for _, a in authorities]
    med_ts = median(ts_vals)
    from datetime import datetime as _dt
    med_dt = _dt.fromtimestamp(med_ts)
    median_str = med_dt.strftime("%Y:%m:%d %H:%M:%S")

    out: list[Finding] = []
    small = _small_session_paths(authorities)
    for row, authority in authorities:
        delta = authority.dt.timestamp() - med_ts
        if abs(delta) < DRIFT_THRESHOLD_SECONDS:
            continue
        if row.path not in small:
            continue
        days = delta / 86400.0
        this_str = authority.dt.strftime("%Y-%m-%d %H:%M:%S")
        reason = (
            f"{days:+.1f}d off folder median "
            f"(source={authority.source}, this={this_str}, median={med_dt.strftime('%Y-%m-%d %H:%M:%S')})"
        )
        out.append(Finding(
            rule="clock-drift",
            confidence="medium",
            path=row.path,
            action="write_xmp",
            patch={"DateTimeOriginal": median_str},
            reason=reason,
        ))
    return out


register(Rule(name="clock-drift", confidence="medium", propose=_propose))
