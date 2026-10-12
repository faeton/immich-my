"""Data model shared by scan, decisions, render and the viewer.

An *event* is a folder of Tesla segments (one ~60 s mp4 per camera per
segment) with an *event timeline*: segment k starts at `start` seconds, the
timeline runs 0..`total`. The save trigger sits at `trigger_s` (None for
RecentClips drives, which have no trigger).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime

CAMERAS = ("front", "left_pillar", "right_pillar", "left_repeater", "right_repeater", "back")

SEG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-"
    r"(?P<cam>front|back|left_repeater|right_repeater|left_pillar|right_pillar)\.mp4$"
)
EVENT_DIR_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:__(?P<slug>.*))?$")

REASON_SLUGS = {
    "user_interaction_honk": "honk",
    "user_interaction_dashcam_icon_tapped": "tapped",
    "user_interaction_dashcam_launcher_action_tapped": "tapped",
    "user_interaction_dashcam_panel_save": "tapped",
    "vehicle_auto_emergency_braking": "aeb",
    "sentry_aware_object_detection": "sentry",
    "sentry_aware_accel": "sentry",
    "saved_no_meta": "saved",
    "sentry_no_meta": "sentry",
    "drive": "drive",
}

# Default review window around the trigger, per kind (seconds).
DEFAULT_WINDOW = {"saved": (180.0, 30.0), "sentry": (60.0, 60.0)}

DRIVE_GAP_S = 120.0        # RecentClips: a gap > this starts a new drive
DRIVE_CHAPTER_S = 900.0    # …and a drive is cut into chapters of at most this


def parse_local(ts: str) -> datetime:
    """`YYYY-MM-DD_HH-MM-SS` (the car's local clock) → naive datetime."""
    return datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")


def reason_slug(reason: str | None, kind: str) -> str:
    if not reason:
        return "sentry" if kind == "sentry" else ("drive" if kind == "recent" else "saved")
    if reason in REASON_SLUGS:
        return REASON_SLUGS[reason]
    if reason.startswith("sentry"):
        return "sentry"
    if reason.startswith("user_interaction"):
        return "tapped"
    return "unknown"


@dataclass
class Segment:
    t: str                      # local clock stamp from the filename
    d: float                    # duration (s)
    start: float                # offset on the event timeline (s)
    f: dict[str, str]           # camera → filename
    utc: str | None = None      # creation_time of the reference file (UTC ISO, no offset)
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None  # of the reference (front) file, bits/s
    # Cameras whose file is shorter than the segment (camera → its own duration, s).
    # The 2023 HW4 front camera records only the first ~38 s of each minute, in
    # real time, then nothing until the next segment: a hole, not a slow clock.
    cd: dict[str, float] | None = None

    @property
    def end(self) -> float:
        return self.start + self.d

    def cam_d(self, cam: str) -> float:
        """How many seconds of the segment this camera's file covers (from its start)."""
        return (self.cd or {}).get(cam, self.d)


@dataclass
class Event:
    id: str
    dump: str
    kind: str                   # saved | sentry | recent
    dir: str                    # relative to the dumps root
    cams: list[str]
    segs: list[Segment]
    total: float
    trigger_s: float | None
    ts: str                     # local wall time of the trigger / drive start, ISO (no offset)
    offset: str | None          # "+02:00"
    offset_source: str          # gps | folder | none
    lat: float | None
    lon: float | None
    gps_source: str             # sei | json | prev-drive | none
    city: str
    reason: str
    reason_slug: str
    sei: bool
    bad: int
    thumb: str
    speed_kmh: int | None = None
    tz: str | None = None       # IANA zone when known
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for s in d["segs"]:
            if not s.get("cd"):
                s.pop("cd", None)      # only segments with a short camera carry it
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d)
        d["segs"] = [Segment(**s) for s in d["segs"]]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def seg_at(self, t: float) -> Segment:
        """Segment containing timeline second `t` (last one for t ≥ total)."""
        for s in self.segs:
            if t < s.end:
                return s
        return self.segs[-1]

    def utc_at(self, t: float) -> datetime | None:
        """UTC instant for timeline second `t`, from the segment's creation_time."""
        s = self.seg_at(t)
        base = s.utc
        if base is None:
            # fall back to any segment with a clock, shifted by the timeline
            for o in self.segs:
                if o.utc:
                    return datetime.fromisoformat(o.utc) + _td(t - o.start)
            return None
        return datetime.fromisoformat(base) + _td(t - s.start)

    def overlapping(self, a: float, b: float) -> list[Segment]:
        return [s for s in self.segs if s.end > a and s.start < b]

    def snapped(self, a: float, b: float) -> tuple[float, float]:
        """[a,b] widened outward to whole segments."""
        segs = self.overlapping(a, b)
        if not segs:
            return a, b
        return segs[0].start, segs[-1].end


def _td(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=seconds)


def default_window(ev: Event) -> tuple[float, float]:
    if ev.kind == "recent" or ev.trigger_s is None:
        return 0.0, ev.total
    before, after = DEFAULT_WINDOW.get(ev.kind, DEFAULT_WINDOW["saved"])
    return max(0.0, ev.trigger_s - before), min(ev.total, ev.trigger_s + after)


def clamp_window(ev: Event, a: float | None, b: float | None) -> tuple[float, float]:
    da, db = default_window(ev)
    a = da if a is None else max(0.0, min(float(a), ev.total))
    b = db if b is None else max(0.0, min(float(b), ev.total))
    if b <= a:
        a, b = da, db
    return a, b
