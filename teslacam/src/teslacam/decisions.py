"""Review decisions: one `decisions-<dump>.json` per dump under the review dir.

The render is a pure function of events.json + these files + the renderer
version, so they are the only moderation state. Written atomically.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .model import Event, clamp_window

KEEP_STATES = ("unreviewed", "keep", "drop")
FIELDS = ("keep", "cameras", "in_s", "out_s", "precise", "tags", "note", "archive",
          "lat", "lon", "city", "updated")


@dataclass
class Decision:
    keep: str = "unreviewed"
    cameras: list[str] | None = None     # None = all present cameras
    in_s: float | None = None
    out_s: float | None = None
    precise: bool = False
    tags: list[str] = field(default_factory=list)
    note: str = ""
    archive: bool = False
    # A location set by hand for an event the car could not place (no SEI
    # telemetry, no event.json, no located drive nearby). Overrides the
    # scanned GPS; `city` is optional and only labels the description.
    lat: float | None = None
    lon: float | None = None
    city: str = ""
    updated: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        return cls(**{k: v for k, v in d.items() if k in FIELDS})


def validate(d: dict) -> dict:
    """Validate a partial decision (API body); raise ValueError on junk."""
    out: dict = {}
    for k, v in d.items():
        if k not in FIELDS or k == "updated":
            raise ValueError(f"unknown field {k!r}")
        if k == "keep":
            if v not in KEEP_STATES:
                raise ValueError(f"keep must be one of {KEEP_STATES}")
        elif k == "cameras":
            if v is not None and (not isinstance(v, list) or not all(isinstance(c, str) for c in v)):
                raise ValueError("cameras must be null or a list of camera names")
        elif k in ("in_s", "out_s"):
            if v is not None:
                v = float(v)
                if v < 0:
                    raise ValueError(f"{k} must be ≥ 0")
        elif k in ("precise", "archive"):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be a bool")
        elif k == "tags":
            if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
                raise ValueError("tags must be a list of strings")
            v = [t.strip() for t in v if t.strip()]
        elif k == "note":
            if not isinstance(v, str):
                raise ValueError("note must be a string")
        elif k in ("lat", "lon"):
            if v is not None:
                v = float(v)
                limit = 90.0 if k == "lat" else 180.0
                if not -limit <= v <= limit:
                    raise ValueError(f"{k} out of range")
        elif k == "city":
            if not isinstance(v, str):
                raise ValueError("city must be a string")
            v = v.strip()
        out[k] = v
    if ("lat" in out) != ("lon" in out):
        raise ValueError("lat and lon come together")
    if "lat" in out and out["lat"] is None:
        out["city"] = ""   # clearing the coordinates clears the hand-set label too
    return out


def placed(ev: Event, d: Decision | None) -> Event:
    """The event as the render sees it: a hand-set location overrides the
    scanned one (source `manual`); nothing else changes."""
    if d is None or d.lat is None or d.lon is None:
        return ev
    return replace(ev, lat=d.lat, lon=d.lon, gps_source="manual", city=d.city or ev.city)


def decisions_path(review_dir: Path, dump: str) -> Path:
    return review_dir / f"decisions-{dump}.json"


def load_dump(review_dir: Path, dump: str) -> dict[str, Decision]:
    p = decisions_path(review_dir, dump)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {eid: Decision.from_dict(v) for eid, v in (data.get("events") or {}).items()}


def load_all(review_dir: Path) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for p in sorted(review_dir.glob("decisions-*.json")):
        data = json.loads(p.read_text())
        for eid, v in (data.get("events") or {}).items():
            out[eid] = Decision.from_dict(v)
    return out


def save_dump(review_dir: Path, dump: str, decisions: dict[str, Decision]) -> None:
    review_dir.mkdir(parents=True, exist_ok=True)
    p = decisions_path(review_dir, dump)
    body = {"schema": 1, "dump": dump,
            "events": {eid: d.to_dict() for eid, d in sorted(decisions.items())}}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def update(review_dir: Path, event_id: str, patch: dict) -> Decision:
    """Merge a validated patch into the event's decision and persist."""
    dump = event_id.split("|", 1)[0]
    current = load_dump(review_dir, dump)
    d = current.get(event_id, Decision())
    for k, v in validate(patch).items():
        setattr(d, k, v)
    d.updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current[event_id] = d
    save_dump(review_dir, dump, current)
    return d


def effective(ev: Event, d: Decision | None) -> tuple[float, float, list[str]]:
    """(in_s, out_s, cameras) the render will use."""
    d = d or Decision()
    a, b = clamp_window(ev, d.in_s, d.out_s)
    cams = [c for c in ev.cams if d.cameras is None or c in d.cameras]
    return a, b, cams


def seed_from_tags_json(review_dir: Path, tags_json: Path, *, overwrite: bool = False) -> int:
    """One-time import of the old viewer's `tags.json`
    (`{id: {tags, note, in, out}}`). Returns the number of events seeded."""
    old = json.loads(tags_json.read_text() or "{}")
    by_dump: dict[str, dict[str, Decision]] = {}
    n = 0
    for eid, rec in old.items():
        dump = eid.split("|", 1)[0]
        cur = by_dump.setdefault(dump, load_dump(review_dir, dump))
        if eid in cur and not overwrite:
            continue
        d = cur.get(eid, Decision())
        d.tags = list(rec.get("tags") or [])
        d.note = rec.get("note") or ""
        d.in_s = rec.get("in")
        d.out_s = rec.get("out")
        d.keep = "keep" if (d.tags or d.note) else d.keep
        d.updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur[eid] = d
        n += 1
    for dump, cur in by_dump.items():
        save_dump(review_dir, dump, cur)
    return n
