"""Index the dumps: events (Saved / Sentry / flat 2024 layout / RecentClips
drives), segment map, durations, clocks, SEI GPS, timezone, thumbnails.
Writes `<review_dir>/events.json`. Read-only on the dumps.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import sei as sei_mod
from .model import (CAMERAS, DRIVE_CHAPTER_S, DRIVE_GAP_S, EVENT_DIR_RE, SEG_RE,
                    Event, Segment, parse_local, reason_slug)

KINDS = {"SavedClips": "saved", "SentryClips": "sentry"}
DEFAULT_EXCLUDE = ("TeslaCam",)      # the still-encrypted USB originals (duplicate of Sep15)
PREV_DRIVE_MAX_AGE_S = 6 * 3600
MAX_ABS_OFFSET_H = 14


# ---------- ffprobe (cached) ----------

class ProbeCache:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}
        self.dirty = False

    def probe(self, root: Path, rel: str) -> dict | None:
        p = root / rel
        try:
            key = f"{rel}|{p.stat().st_size}"
        except OSError:
            return None
        if key in self.data:
            return self.data[key]
        info = ffprobe(p)
        self.data[key] = info
        self.dirty = True
        return info

    def flush(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)
            self.dirty = False


def ffprobe(path: Path) -> dict | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration:format_tags=creation_time:stream=width,height,bit_rate,avg_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return None
        j = json.loads(out.stdout or "{}")
        fmt = j.get("format") or {}
        st = (j.get("streams") or [{}])[0]
        dur = float(fmt.get("duration") or 0) or None
        ct = (fmt.get("tags") or {}).get("creation_time")
        utc = None
        if ct:
            utc = ct[:19]            # "2026-09-09T17:06:19.000000Z" → naive UTC
        fr = st.get("avg_frame_rate") or "0/1"
        try:
            n, d = fr.split("/")
            fps = float(n) / float(d) if float(d) else None
        except Exception:
            fps = None
        return {"d": dur, "utc": utc, "w": st.get("width"), "h": st.get("height"),
                "br": int(st["bit_rate"]) if st.get("bit_rate") else None, "fps": fps}
    except Exception:
        return None


# ---------- discovery ----------

def _segments_in(root: Path, ev_dir: Path) -> dict[str, dict[str, str]]:
    segs: dict[str, dict[str, str]] = {}
    for f in sorted(ev_dir.iterdir()):
        m = SEG_RE.match(f.name)
        if m:
            segs.setdefault(m["ts"], {})[m["cam"]] = f.name
    return segs


def _read_event_json(p: Path) -> dict | None:
    try:
        j = json.loads(p.read_text())
        return j if isinstance(j, dict) else None
    except Exception:
        return None            # still encrypted / garbage


def _event_dirs(root: Path, dump: Path):
    """Yield (kind, ev_dir) for a dump in either layout."""
    for kind_dir, kind in KINDS.items():
        kd = dump / kind_dir
        if kd.is_dir():
            for ev in sorted(p for p in kd.iterdir() if p.is_dir()):
                yield kind, ev
    # flat layout (TeslaDriveCam): <dump>/<ts>__<city>__<reason>/
    for ev in sorted(p for p in dump.iterdir() if p.is_dir() and EVENT_DIR_RE.match(p.name)):
        ej = _read_event_json(ev / "event.json") or {}
        reason = ej.get("reason") or ""
        yield ("sentry" if reason.startswith("sentry") else "saved"), ev


SHORT_CAM_S = 1.0     # a camera this much shorter than the segment has a hole at its end


def _build_segments(root: Path, rel_dir: str, segmap: dict[str, dict[str, str]],
                    cache: ProbeCache) -> tuple[list[Segment], int]:
    """The segment lasts as long as its LONGEST camera file: one camera can stop
    early (2023 HW4 front: ~38 s of each minute), never run long. Geometry, bitrate
    and clock still come from the front file when it is readable (the mosaic tile
    size and the precise re-encode use them)."""
    segs: list[Segment] = []
    bad = 0
    t = 0.0
    for ts in sorted(segmap):
        files = segmap[ts]
        ref_cam = "front" if "front" in files else next(iter(files))
        infos: dict[str, dict] = {}
        for cam, fn in files.items():
            info = cache.probe(root, f"{rel_dir}/{fn}")
            if info is None or not info.get("d"):
                bad += 1
                continue
            infos[cam] = info
        ref = infos.get(ref_cam) or next(iter(infos.values()), None)
        longest = max((float(i["d"]) for i in infos.values()), default=0.0)
        # The reference camera's length unless it is clearly short: unaffected events
        # keep exactly the durations (and so the render digests) they had before.
        d = float(ref["d"]) if ref and longest - float(ref["d"]) <= SHORT_CAM_S else longest
        short = {cam: round(float(i["d"]), 3) for cam, i in infos.items() if d - float(i["d"]) > SHORT_CAM_S}
        segs.append(Segment(t=ts, d=round(d, 3), start=round(t, 3), f=dict(sorted(files.items())),
                            utc=(ref or {}).get("utc"), width=(ref or {}).get("w"),
                            height=(ref or {}).get("h"), bitrate=(ref or {}).get("br"),
                            cd=dict(sorted(short.items())) or None))
        t += d
    return segs, bad


def _cams(segs: list[Segment]) -> list[str]:
    present = {c for s in segs for c in s.f}
    return [c for c in CAMERAS if c in present]


# ---------- time / GPS / tz ----------

def _fmt_offset(td: timedelta) -> str:
    secs = int(round(td.total_seconds()))
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    return f"{sign}{secs // 3600:02d}:{(secs % 3600) // 60:02d}"


def offset_from_gps(lat: float, lon: float, utc: datetime) -> tuple[str, str] | None:
    """(offset, IANA zone) for the instant, via timezonefinder."""
    try:
        from timezonefinder import TimezoneFinder
    except Exception:
        return None
    tf = _tf_singleton(TimezoneFinder)
    zone = tf.timezone_at(lat=lat, lng=lon)
    if not zone:
        return None
    off = ZoneInfo(zone).utcoffset(utc.replace(tzinfo=timezone.utc))
    if off is None:
        return None
    return _fmt_offset(off), zone


_TF = None


def _tf_singleton(cls):
    global _TF
    if _TF is None:
        _TF = cls(in_memory=True)
    return _TF


def offset_from_folder(local: datetime, utc: datetime) -> str | None:
    """Car's local clock (folder/segment name) minus the file's UTC clock,
    rounded to 15 min; rejected beyond ±14 h."""
    delta = (local - utc).total_seconds()
    q = round(delta / 900) * 900
    if abs(q) > MAX_ABS_OFFSET_H * 3600:
        return None
    return _fmt_offset(timedelta(seconds=q))


def _front_sei(root: Path, ev_dir_rel: str, seg: Segment) -> list[dict]:
    fn = seg.f.get("front")
    if not fn:
        return []
    try:
        return sei_mod.sei_track(root / ev_dir_rel / fn, max_samples=5000)
    except Exception:
        return []


def _trigger_segment_sei(root: Path, ev_dir_rel: str, segs: list[Segment], trigger_s: float | None):
    """SEI samples of the front file of the segment holding the trigger
    (last segment for drives). When that segment has no valid fix — the car
    writes no SEI at all while it has no GPS, so a drive that lost the
    signal part-way ends blank — the nearest segment with a fix stands in,
    so the event is still placed. A read costs ~20 ms per file."""
    if not segs:
        return []
    t = segs[-1].start if trigger_s is None else trigger_s
    i = next((k for k, s in enumerate(segs) if t < s.end), len(segs) - 1)
    samples = _front_sei(root, ev_dir_rel, segs[i])
    if sei_mod.median_fix(samples):
        return samples
    for k in sorted((k for k in range(len(segs)) if k != i), key=lambda k: (abs(k - i), k)):
        other = _front_sei(root, ev_dir_rel, segs[k])
        if sei_mod.median_fix(other):
            return other
    return samples


# ---------- reverse geocode (opt-in, cached) ----------

class GeoCache:
    """`<review_dir>/geocache.json`: "lat,lon" (3 decimals) → city. Seeded
    from the old `_tools/geocache.json` when present at the dumps root."""

    def __init__(self, path: Path, seed: Path | None = None):
        self.path = path
        self.data: dict[str, str | None] = {}
        for p in (seed, path):
            if p and p.exists():
                try:
                    self.data.update(json.loads(p.read_text()))
                except Exception:
                    pass
        self.dirty = False

    @staticmethod
    def key(lat: float, lon: float) -> str:
        return f"{lat:.3f},{lon:.3f}"

    def lookup(self, lat: float, lon: float) -> str | None:
        key = self.key(lat, lon)
        if key in self.data:
            return self.data[key]
        city = nominatim_city(lat, lon)
        self.data[key] = city
        self.dirty = True
        return city

    def flush(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
            tmp.replace(self.path)
            self.dirty = False


def nominatim_city(lat: float, lon: float) -> str | None:
    """One polite Nominatim reverse lookup (1 req/s, personal use)."""
    import time
    import urllib.parse
    import urllib.request
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 10, "accept-language": "en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "teslacam/0.1 (personal dashcam catalogue)"})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read())
        a = j.get("address", {})
        city = (a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
                or a.get("county") or j.get("name"))
        time.sleep(1.1)
        return city
    except Exception:
        return None


# ---------- thumbnails ----------

def make_thumb(root: Path, ev: Event, out: Path) -> None:
    if out.exists() and out.stat().st_size > 0:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    t = ev.total - 2.0 if ev.trigger_s is None else max(0.0, ev.trigger_s - 1.0)
    seg = ev.seg_at(t)
    # front, unless the trigger falls in its hole: then the longest camera
    cam = "front" if "front" in seg.f and t - seg.start < seg.cam_d("front") else \
        max(seg.f, key=lambda c: (seg.cam_d(c), c == "back"))
    fn = seg.f[cam]
    at = max(0.0, min(t - seg.start, max(0.0, seg.cam_d(cam) - 0.5)))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(root / ev.dir / fn),
                    "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(out)],
                   capture_output=True)
    if out.exists() and out.stat().st_size > 0:
        return
    png = root / ev.dir / "thumb.png"
    if png.exists() and png.read_bytes()[:4] == b"\x89PNG":
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(png), "-vf", "scale=640:-2",
                        "-q:v", "4", str(out)], capture_output=True)


# ---------- events ----------

def _slug_id(eid: str) -> str:
    return eid.replace("|", "__")


def build_event(root: Path, dump: str, kind: str, ev_dir: Path, cache: ProbeCache,
                *, kind_dir: str | None) -> Event | None:
    rel_dir = str(ev_dir.relative_to(root))
    segmap = _segments_in(root, ev_dir)
    if not segmap:
        return None
    segs, bad = _build_segments(root, rel_dir, segmap, cache)
    total = round(sum(s.d for s in segs), 3)
    m = EVENT_DIR_RE.match(ev_dir.name)
    folder_ts = m["ts"] if m else segs[0].t
    ej = _read_event_json(ev_dir / "event.json")
    eid = f"{dump}|{kind_dir or ''}|{folder_ts}" if kind_dir else f"{dump}|{ev_dir.name}"

    # trigger on the timeline: car-local json timestamp (else folder name) vs first segment
    trig_local = None
    if ej and ej.get("timestamp"):
        try:
            trig_local = datetime.fromisoformat(str(ej["timestamp"])[:19])
        except ValueError:
            trig_local = None
    if trig_local is None:
        trig_local = parse_local(folder_ts)
    trigger_s = (trig_local - parse_local(segs[0].t)).total_seconds()
    trigger_s = max(0.0, min(total, trigger_s))
    flags: list[str] = []
    if bad:
        flags.append("bad-files")
    flags += [f"short-{c}" for c in sorted({c for s in segs for c in (s.cd or {})})]

    # GPS
    lat = lon = None
    gps_source = "none"
    samples = _trigger_segment_sei(root, rel_dir, segs, trigger_s)
    sei_present = bool(samples)
    fix = sei_mod.median_fix(samples) if samples else None
    speed = None
    if fix:
        lat, lon, gps_source = fix["lat"], fix["lon"], "sei"
        if fix.get("speed_mps") is not None:
            speed = int(round(fix["speed_mps"] * 3.6))
    elif ej:
        try:
            la, lo = float(ej.get("est_lat")), float(ej.get("est_lon"))
            if abs(la) > 0.01 and abs(lo) > 0.01:
                lat, lon, gps_source = round(la, 6), round(lo, 6), "json"
        except (TypeError, ValueError):
            pass

    reason = (ej or {}).get("reason") or ("saved_no_meta" if kind == "saved" else "sentry_no_meta")
    city = (ej or {}).get("city") or ""

    ev = Event(id=eid, dump=dump, kind=kind, dir=rel_dir, cams=_cams(segs), segs=segs, total=total,
               trigger_s=round(trigger_s, 3), ts=trig_local.strftime("%Y-%m-%dT%H:%M:%S"),
               offset=None, offset_source="none", lat=lat, lon=lon, gps_source=gps_source,
               city=city, reason=reason, reason_slug=reason_slug(reason, kind), sei=sei_present,
               bad=bad, thumb=f"thumbs/{_slug_id(eid)}.jpg", speed_kmh=speed, flags=flags)
    _resolve_offset(ev, trig_local)
    return ev


def _resolve_offset(ev: Event, local: datetime) -> None:
    utc = ev.utc_at(ev.trigger_s if ev.trigger_s is not None else 0.0)
    if utc is None:
        ev.flags.append("no-clock")
        return
    if ev.lat is not None and ev.lon is not None:
        r = offset_from_gps(ev.lat, ev.lon, utc)
        if r:
            ev.offset, ev.tz = r
            ev.offset_source = "gps"
            # the car's clock may disagree with the zone: show the true local time
            ev.ts = (utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(r[1]))
                     .strftime("%Y-%m-%dT%H:%M:%S"))
            return
    off = offset_from_folder(local, utc)
    if off:
        ev.offset, ev.offset_source = off, "folder"
        return
    ev.offset, ev.offset_source = None, "none"
    ev.flags.append("tz-unknown")


def build_drives(root: Path, dump: str, recent_dir: Path, cache: ProbeCache) -> list[Event]:
    """RecentClips → drives split on gaps > DRIVE_GAP_S, chapters ≤ DRIVE_CHAPTER_S."""
    segmap = _segments_in(root, recent_dir)
    if not segmap:
        return []
    rel_dir = str(recent_dir.relative_to(root))
    all_segs, _bad = _build_segments(root, rel_dir, segmap, cache)
    groups: list[list[Segment]] = []
    prev_end_local: datetime | None = None
    for s in all_segs:
        start_local = parse_local(s.t)
        if (groups and prev_end_local is not None
                and (start_local - prev_end_local).total_seconds() <= DRIVE_GAP_S
                and sum(x.d for x in groups[-1]) + s.d <= DRIVE_CHAPTER_S):
            groups[-1].append(s)
        else:
            groups.append([s])
        prev_end_local = start_local + timedelta(seconds=s.d)
    events: list[Event] = []
    for g in groups:
        t = 0.0
        segs = []
        for s in g:
            segs.append(replace(s, start=round(t, 3)))
            t += s.d
        total = round(t, 3)
        eid = f"{dump}|RecentClips|{g[0].t}"
        local = parse_local(g[0].t)
        samples = _trigger_segment_sei(root, rel_dir, segs, None)
        fix = sei_mod.median_fix(samples) if samples else None
        ev = Event(id=eid, dump=dump, kind="recent", dir=rel_dir, cams=_cams(segs), segs=segs,
                   total=total, trigger_s=None, ts=local.strftime("%Y-%m-%dT%H:%M:%S"),
                   offset=None, offset_source="none",
                   lat=fix["lat"] if fix else None, lon=fix["lon"] if fix else None,
                   gps_source="sei" if fix else "none", city="", reason="drive",
                   reason_slug="drive", sei=bool(samples), bad=0,
                   thumb=f"thumbs/{_slug_id(eid)}.jpg",
                   speed_kmh=int(round(fix["speed_mps"] * 3.6)) if fix and fix.get("speed_mps") else None)
        _resolve_offset(ev, local)
        events.append(ev)
    return events


def _prev_drive_gps(events: list[Event]) -> None:
    """Sentry/no-GPS events borrow the last fix of the previous located
    event in the same dump, if it is under PREV_DRIVE_MAX_AGE_S old."""
    by_dump: dict[str, list[Event]] = {}
    for ev in events:
        by_dump.setdefault(ev.dump, []).append(ev)
    for evs in by_dump.values():
        evs.sort(key=lambda e: e.utc_at(e.trigger_s or 0.0) or datetime.min)
        last: tuple[datetime, float, float] | None = None
        for ev in evs:
            t = ev.utc_at(ev.trigger_s or 0.0)
            if ev.lat is not None and ev.gps_source in ("sei", "json") and t is not None:
                last = (t, ev.lat, ev.lon)
                continue
            if ev.lat is None and last and t is not None and 0 <= (t - last[0]).total_seconds() <= PREV_DRIVE_MAX_AGE_S:
                ev.lat, ev.lon, ev.gps_source = last[1], last[2], "prev-drive"
                if ev.offset_source != "gps":
                    _resolve_offset(ev, datetime.fromisoformat(ev.ts))


def scan(root: Path, review_dir: Path, *, exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
         thumbs: bool = True, geocode: bool = False,
         log: Callable[[str], None] = lambda s: None) -> dict:
    root = root.resolve()
    review_dir.mkdir(parents=True, exist_ok=True)
    cache = ProbeCache(review_dir / "probe-cache.json")
    events: list[Event] = []
    seen: dict[str, str] = {}       # segment filename → event id (names are unique per car)
    duplicates: list[dict] = []
    dumps = sorted(p for p in root.iterdir()
                   if p.is_dir() and not p.name.startswith(("_", ".")) and p.name not in exclude)
    for dump in dumps:
        for kind, ev_dir in _event_dirs(root, dump):
            kind_dir = ev_dir.parent.name if ev_dir.parent != dump else None
            ev = build_event(root, dump.name, kind, ev_dir, cache, kind_dir=kind_dir)
            if ev is None:
                continue
            names = [fn for s in ev.segs for fn in s.f.values()]
            dup_of = {seen[n] for n in names if n in seen}
            if dup_of and all(n in seen for n in names):
                duplicates.append({"id": ev.id, "duplicate_of": sorted(dup_of)})
                log(f"  dup  {ev.id} = {sorted(dup_of)}")
                continue
            for n in names:
                seen.setdefault(n, ev.id)
            events.append(ev)
            log(f"  {ev.id}  {ev.city or '—'}  {ev.reason_slug}  segs={len(ev.segs)} gps={ev.gps_source} tz={ev.offset_source}")
        recent = dump / "RecentClips"
        if recent.is_dir():
            for ev in build_drives(root, dump.name, recent, cache):
                names = [fn for s in ev.segs for fn in s.f.values()]
                if names and all(n in seen for n in names):
                    duplicates.append({"id": ev.id, "duplicate_of": sorted({seen[n] for n in names})})
                    continue
                for n in names:
                    seen.setdefault(n, ev.id)
                events.append(ev)
                log(f"  {ev.id}  drive {ev.total/60:.1f} min  segs={len(ev.segs)} gps={ev.gps_source}")
        cache.flush()
    _prev_drive_gps(events)
    if geocode:
        gc = GeoCache(review_dir / "geocache.json", seed=root / "_tools" / "geocache.json")
        for ev in events:
            if not ev.city and ev.lat is not None and ev.lon is not None:
                ev.city = gc.lookup(ev.lat, ev.lon) or ""
        gc.flush()
    if thumbs:
        for ev in events:
            make_thumb(root, ev, review_dir / ev.thumb)
    cache.flush()
    events.sort(key=lambda e: e.ts, reverse=True)
    doc = {"schema": 1, "root": str(root),
           "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "events": [e.to_dict() for e in events], "duplicates": duplicates}
    out = review_dir / "events.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False))
    tmp.replace(out)
    return doc


def load_events(review_dir: Path) -> tuple[Path, list[Event]]:
    doc = json.loads((review_dir / "events.json").read_text())
    return Path(doc["root"]), [Event.from_dict(e) for e in doc["events"]]
