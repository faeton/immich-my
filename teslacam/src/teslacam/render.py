"""Render kept events into a promote-able folder.

Per event (relative to `<render_root>/<YYYY>-tesla-dashcam/<slug>/`):
  mosaic.mp4            all cameras tiled, exact [in,out], H.264 proxy ≤ 720 rows
  <camera>.mp4          every ticked camera, lossless `-c copy` cut of [in,out]
                        at keyframe granularity (starts ≤ 0.75 s before `in`;
                        + wall-clock subtitle track); `precise` re-encodes to
                        the exact range
  track.gpx/.json       SEI GPS, 1 Hz, when present
  event.json            copy of the car's metadata
  render.json           receipt: inputs + decision hash → idempotent re-runs
Folder root: stacks.json manifest for `immy stacks apply`.

Metadata is baked into every mp4 with exiftool (date+offset, Make/Model,
description, GPS on every file) so Immich reads it from the container.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import track as track_mod
from .decisions import Decision, effective, placed
from .model import CAMERAS, Event, Segment

RENDER_VERSION = "v2"   # v2: children cut at keyframes instead of whole segments
STAMP_VERSION = 2       # 2: GPS on every file, not only the mosaic (a bump re-stamps, no re-encode)
MOSAIC_ROWS = 720
MOSAIC_BITRATE = "4M"
MOSAIC_FPS = 30
LAYOUT6 = [["left_pillar", "front", "right_pillar"], ["left_repeater", "back", "right_repeater"]]
LAYOUT4 = [["front", "back"], ["left_repeater", "right_repeater"]]
CAM_LABELS = {"front": "FRONT", "back": "REAR", "left_repeater": "LEFT", "right_repeater": "RIGHT",
              "left_pillar": "L-PILLAR", "right_pillar": "R-PILLAR"}
FONT_CANDIDATES = ("/System/Library/Fonts/Monaco.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
EVENT_MARK_BEFORE, EVENT_MARK_AFTER = 1.0, 6.0


def slugify(value: str) -> str:
    value = value.translate(str.maketrans({"ß": "ss", "ł": "l", "Ł": "l", "đ": "d", "ø": "o", "æ": "ae"}))
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-") or "unknown"


def _h264_encoder() -> list[str]:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except Exception:
        out = ""
    if "h264_videotoolbox" in out:
        return ["-c:v", "h264_videotoolbox", "-b:v", MOSAIC_BITRATE, "-maxrate", "5M", "-bufsize", "8M",
                "-profile:v", "high", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "5M", "-bufsize", "8M",
            "-profile:v", "high"]


_FILTERS: str | None = None


def _has_filter(name: str) -> bool:
    global _FILTERS
    if _FILTERS is None:
        try:
            _FILTERS = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        except Exception:
            _FILTERS = ""
    return f" {name} " in _FILTERS


def _font() -> str | None:
    """Font for the camera labels; None when ffmpeg lacks drawtext (labels are skipped)."""
    if not _has_filter("drawtext"):
        return None
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


@dataclass
class Job:
    ev: Event
    decision: Decision
    out_dir: Path
    folder: Path                 # <render_root>/<YYYY>-tesla-dashcam
    a: float
    b: float
    cams: list[str]              # children
    snap_a: float
    snap_b: float
    digest: str

    @property
    def slug(self) -> str:
        return self.out_dir.name

    def est_bytes(self) -> int:
        mosaic = int((self.b - self.a) * 4_000_000 / 8)
        children = 0
        for s in self.ev.overlapping(self.a, self.b):
            span = min(s.end, self.b) - max(s.start, self.a)   # children follow the window now
            for c in self.cams:
                if c in s.f:
                    br = s.bitrate or 5_500_000
                    children += int(max(0.0, span) * br / 8)
        return mosaic + children

    def est_seconds(self) -> float:
        # encode speed on VideoToolbox ≈ 6× realtime for a 6-cam decode; copies are I/O only
        return (self.b - self.a) / 6.0 + 2.0


def _digest(ev: Event, d: Decision, a: float, b: float, cams: list[str]) -> str:
    h = hashlib.sha256()
    h.update(RENDER_VERSION.encode())
    for s in ev.segs:
        h.update(f"{s.t}|{s.d}|{sorted(s.f.items())}".encode())
    h.update(json.dumps({"a": a, "b": b, "cams": cams, "precise": d.precise, "tags": d.tags,
                         "note": d.note, "archive": d.archive, "offset": ev.offset,
                         "lat": ev.lat, "lon": ev.lon, "city": ev.city, "reason": ev.reason}).encode())
    return h.hexdigest()[:16]


def local_trigger(ev: Event) -> datetime:
    return datetime.fromisoformat(ev.ts)


def plan(events: list[Event], decisions: dict[str, Decision], render_root: Path, *,
         only_ids: set[str] | None = None, dumps: set[str] | None = None) -> list[Job]:
    jobs: list[Job] = []
    for ev in events:
        if only_ids and ev.id not in only_ids:
            continue
        if dumps and ev.dump not in dumps:
            continue
        d = decisions.get(ev.id) or Decision()
        if d.keep != "keep" and not (only_ids and ev.id in only_ids):
            continue
        ev = placed(ev, d)   # a hand-set location is part of the digest: it re-renders
        a, b, cams = effective(ev, d)
        if b - a < 1.0:
            continue
        lt = local_trigger(ev)
        folder = render_root / f"{lt.year}-tesla-dashcam"
        # The folder name is the asset's identity in Immich (path = identity in
        # an external library), so it must not move when a city, reason or
        # timezone gets corrected later: kind + the car's own folder stamp.
        slug = f"{ev.kind}-{ev.id.rsplit('|', 1)[-1]}"
        sa, sb = ev.snapped(a, b)
        jobs.append(Job(ev=ev, decision=d, out_dir=folder / slug, folder=folder, a=a, b=b, cams=cams,
                        snap_a=sa, snap_b=sb, digest=_digest(ev, d, a, b, cams)))
    return jobs


# ---------- ffmpeg pieces ----------

def _concat_list(root: Path, ev: Event, cam: str, segs: list[Segment], path: Path, *,
                 inpoint: float | None = None, outpoint: float | None = None) -> bool:
    """`inpoint` applies to the first file, `outpoint` to the last (seconds into that file)."""
    lines = []
    for i, s in enumerate(segs):
        fn = s.f.get(cam)
        if not fn:
            return False           # a hole would desync the camera → caller blacks the tile
        lines.append(f"file '{(root / ev.dir / fn).as_posix()}'")
        if i == 0 and inpoint is not None and inpoint > 0:
            lines.append(f"inpoint {inpoint:.3f}")
        if i == len(segs) - 1 and outpoint is not None:
            lines.append(f"outpoint {outpoint:.3f}")
    path.write_text("\n".join(lines) + "\n")
    return True


def keyframes(path: Path) -> list[float]:
    """Keyframe timestamps (s) from the packet list — no decoding, ~0.2 s per 60 s clip."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=120)
    except Exception:
        return []
    kfs = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                kfs.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(kfs)


def snap_to_keyframe(path: Path, t: float) -> float:
    """The last keyframe at or before `t` — where a stream-copy cut really starts."""
    before = [k for k in keyframes(path) if k <= t + 1e-3]
    return max(before) if before else 0.0


def _tile_size(ev: Event, rows: int) -> tuple[int, int]:
    s = ev.segs[0]
    w, h = (s.width or 1448), (s.height or 938)
    th = rows // 2
    tw = int(round(th * w / h / 2)) * 2
    return tw, th


def render_mosaic(root: Path, job: Job, out: Path, tmp: Path, log: Callable[[str], None]) -> None:
    ev = job.ev
    layout = LAYOUT6 if all(c in ev.cams for c in ("left_pillar", "right_pillar")) else LAYOUT4
    cams = [c for row in layout for c in row]
    tw, th = _tile_size(ev, MOSAIC_ROWS)
    segs = ev.overlapping(job.a, job.b)
    off0 = segs[0].start
    ta, tb = job.a - off0, job.b - off0
    font = _font()
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    n_in = 0
    for i, cam in enumerate(cams):
        lst = tmp / f"{cam}.txt"
        if cam in ev.cams and _concat_list(root, ev, cam, segs, lst):
            inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
            chain = (f"[{n_in}:v]trim=start={ta:.3f}:end={tb:.3f},setpts=PTS-STARTPTS,"
                     f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                     f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={MOSAIC_FPS}")
            n_in += 1
        else:
            chain = f"color=c=black:s={tw}x{th}:r={MOSAIC_FPS}:d={job.b - job.a:.3f}"
        if font:
            chain += (f",drawtext=fontfile='{font}':text='{CAM_LABELS.get(cam, cam)}':"
                      f"x=8:y=8:fontsize=18:fontcolor=white@0.85:box=1:boxcolor=black@0.45:boxborderw=4")
        filters.append(f"{chain}[v{i}]")
        labels.append(f"[v{i}]")
    cols = len(layout[0])
    pos = "|".join(f"{(i % cols) * tw}_{(i // cols) * th}" for i in range(len(cams)))
    fc = ";".join(filters) + ";" + "".join(labels) + f"xstack=inputs={len(cams)}:layout={pos}:fill=black[out]"
    part = out.with_name(out.name + ".part.mp4")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", *inputs, "-filter_complex", fc, "-map", "[out]",
           "-an", *_h264_encoder(), "-pix_fmt", "yuv420p", "-t", f"{job.b - job.a:.3f}",
           "-movflags", "+faststart", "-tag:v", "avc1", str(part)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg mosaic failed:\n" + r.stderr[-2000:])
    _verify(part, job.b - job.a, tolerance=1.5)
    os.replace(part, out)


def _srt(ev: Event, segs: list[Segment], a: float, b: float, offset: str | None) -> str:
    """One cue per second: local wall clock, `>>> EVENT <<<` around the trigger.
    `a`/`b` are timeline seconds of the file's start/end."""
    cues = []
    t = a
    n = 0
    while t < b:
        t1 = min(t + 1.0, b)
        utc = ev.utc_at(t)
        if utc is not None:
            local = utc + _offset_td(offset)
            text = local.strftime("%Y-%m-%d %H:%M:%S") + (f" {offset}" if offset else " UTC")
        else:
            text = f"+{t:.0f}s"
        if ev.trigger_s is not None and -EVENT_MARK_BEFORE <= (t - ev.trigger_s) < EVENT_MARK_AFTER:
            text += "   >>> EVENT <<<"
        n += 1
        cues.append(f"{n}\n{_srt_ts(t - a)} --> {_srt_ts(t1 - a)}\n{text}\n")
        t = t1
    return "\n".join(cues)


def _srt_ts(s: float) -> str:
    ms = int(round(max(0.0, s) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _offset_td(offset: str | None) -> timedelta:
    if not offset:
        return timedelta(0)
    sign = -1 if offset.startswith("-") else 1
    hh, mm = offset[1:].split(":")
    return sign * timedelta(hours=int(hh), minutes=int(mm))


class NoFootage(RuntimeError):
    """A ticked camera has no segment inside the window (the car dropped that
    camera for those minutes). The event still renders without that child."""


def render_child(root: Path, job: Job, cam: str, out: Path, tmp: Path) -> tuple[float, float]:
    """Returns the (start, end) timeline seconds the file actually covers."""
    ev = job.ev
    segs = [s for s in ev.overlapping(job.a, job.b) if cam in s.f]
    if not segs:
        raise NoFootage(f"{cam}: no files in the window")
    first, last = segs[0], segs[-1]
    ta = max(0.0, job.a - first.start)
    tb = min(job.b, last.end) - last.start
    lst = tmp / f"child-{cam}.txt"
    srt = tmp / f"child-{cam}.srt"
    part = out.with_name(out.name + ".part.mp4")
    if not job.decision.precise:
        # Lossless: the concat demuxer's inpoint/outpoint with stream copy. The cut
        # starts at the last keyframe before the in point (Tesla: every 0.5–0.75 s),
        # so the file covers [keyframe, out] and the wall clock is built from there.
        ka = snap_to_keyframe(root / ev.dir / first.f[cam], ta)
        sa, sb = first.start + ka, last.start + tb
        _concat_list(root, ev, cam, segs, lst, inpoint=ka,
                     outpoint=tb if tb < last.d - 0.05 else None)
        srt.write_text(_srt(ev, segs, sa, sb, ev.offset))
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-i", str(srt), "-map", "0:v", "-map", "1:s", "-c", "copy", "-c:s", "mov_text",
               "-avoid_negative_ts", "make_zero",
               "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Wall clock",
               "-disposition:s:0", "default", "-movflags", "+faststart", str(part)]
    else:
        sa, sb = first.start + ta, last.start + tb
        _concat_list(root, ev, cam, segs, lst)
        srt.write_text(_srt(ev, segs, sa, sb, ev.offset))
        br = str(segs[0].bitrate or 6_000_000)
        enc = ["-c:v", "h264_videotoolbox", "-b:v", br, "-profile:v", "high", "-allow_sw", "1"] \
            if "h264_videotoolbox" in " ".join(_h264_encoder()) else ["-c:v", "libx264", "-crf", "20"]
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-i", str(srt), "-filter_complex", f"[0:v]trim=start={ta:.3f}:end={ta + (sb - sa):.3f},setpts=PTS-STARTPTS[v]",
               "-map", "[v]", "-map", "1:s", *enc, "-pix_fmt", "yuv420p", "-c:s", "mov_text",
               "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Wall clock",
               "-movflags", "+faststart", str(part)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg {cam} failed:\n" + r.stderr[-2000:])
    _verify(part, sb - sa, tolerance=3.0)
    os.replace(part, out)
    return sa, sb


def _verify(path: Path, expected_s: float, *, tolerance: float) -> None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", str(path)], capture_output=True, text=True)
    j = json.loads(out.stdout or "{}")
    dur = float((j.get("format") or {}).get("duration") or 0)
    vids = [s for s in j.get("streams") or [] if s.get("codec_type") == "video"]
    if len(vids) != 1:
        raise RuntimeError(f"{path.name}: expected 1 video stream, found {len(vids)}")
    if abs(dur - expected_s) > tolerance:
        raise RuntimeError(f"{path.name}: duration {dur:.1f}s, expected {expected_s:.1f}s")


# ---------- metadata ----------

def description(job: Job) -> str:
    ev, d = job.ev, job.decision
    kind = {"saved": "saved", "sentry": "sentry", "recent": "drive"}[ev.kind]
    lt = local_trigger(ev).strftime("%Y-%m-%d %H:%M")
    bits = [f"Tesla dashcam · {ev.reason_slug} ({kind}) · {ev.city or 'unknown place'} · {lt}"]
    if d.tags:
        bits.append(" ".join("#" + t for t in d.tags))
    if d.note:
        bits.append(d.note)
    bits.append(ev.id)
    return "\n".join(bits)


def stamp(job: Job, path: Path, *, start_t: float, gps: bool, model: str) -> None:
    """Bake date/offset, make/model, description (+GPS) into the container."""
    if not shutil.which("exiftool"):
        return
    ev = job.ev
    utc = ev.utc_at(start_t)
    # No `-api QuickTimeUTC`: QuickTime:CreateDate is UTC by spec and exiftool
    # writes the string verbatim; with the option on it would shift the value
    # through the Mac's local zone. Keys:CreationDate carries the local+offset.
    args = ["exiftool", "-overwrite_original", "-q", "-q"]
    if utc is not None:
        local = utc + _offset_td(ev.offset)
        off = ev.offset or "+00:00"
        args += [f"-QuickTime:CreateDate={utc.strftime('%Y:%m:%d %H:%M:%S')}",
                 f"-QuickTime:ModifyDate={utc.strftime('%Y:%m:%d %H:%M:%S')}",
                 f"-Keys:CreationDate={local.strftime('%Y:%m:%d %H:%M:%S')}{off}"]
    # Model is only written when known ("Tesla" alone is the make); an empty
    # `-Keys:Model=` clears a value left by an earlier stamp.
    args += ["-Keys:Make=Tesla", f"-Keys:Model={model}", f"-Keys:Description={description(job)}",
             f"-Keys:Title={ev.reason_slug} · {ev.city or ''}".rstrip(" ·")]
    if gps and ev.lat is not None and ev.lon is not None:
        args.append(f"-Keys:GPSCoordinates={ev.lat} {ev.lon}")
    subprocess.run(args + [str(path)], check=False, capture_output=True)


# ---------- manifest ----------

def tags_for(job: Job, model: str) -> list[str]:
    ev, d = job.ev, job.decision
    kind = {"saved": "Saved", "sentry": "Sentry", "recent": "Recent"}[ev.kind]
    tags = ["Source/Tesla", f"Gear/Camera/Tesla {model}".rstrip(), f"Tesla/{kind}",
            f"Tesla/Reason/{ev.reason_slug}"]
    if ev.offset_source == "none":
        tags.append("Tesla/TzUnknown")
    tags += [f"Tesla/{t}" for t in d.tags]
    return list(dict.fromkeys(tags))


def stale_dirs(folder: Path, kept_ids: set[str]) -> list[Path]:
    """Rendered event folders whose event is no longer Keep (or was renamed)."""
    out = []
    for rec_path in sorted(folder.glob("*/render.json")):
        try:
            rec = json.loads(rec_path.read_text())
        except Exception:
            out.append(rec_path.parent)
            continue
        if rec.get("event") not in kept_ids or rec.get("slug") != rec_path.parent.name:
            out.append(rec_path.parent)
    return out


def write_manifest(folder: Path, kept_ids: set[str], *, album: str) -> Path:
    """Rebuild stacks.json from the receipts of every kept event in the
    folder (so a partial run still produces a complete manifest for what
    exists, and a dropped event leaves the manifest)."""
    groups = []
    for rec_path in sorted(folder.glob("*/render.json")):
        rec = json.loads(rec_path.read_text())
        if rec.get("event") not in kept_ids or rec.get("slug") != rec_path.parent.name:
            continue
        groups.append({"key": rec["key"], "primary": rec["primary"], "children": rec["children"],
                       "tags": rec["tags"], "archive": rec["archive"]})
    doc = {"schema": 1, "album": album, "groups": groups}
    out = folder / "stacks.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    os.replace(tmp, out)
    return out


# ---------- driver ----------

def _receipt(job: Job) -> dict | None:
    rec = job.out_dir / "render.json"
    if not rec.exists():
        return None
    try:
        r = json.loads(rec.read_text())
    except Exception:
        return None
    if r.get("digest") != job.digest:
        return None
    files = [r["primary"].split("/", 1)[1]] + [c.split("/", 1)[1] for c in r["children"]]
    return r if all((job.out_dir / p).exists() for p in files) else None


def is_done(job: Job, model: str | None = None) -> bool:
    """Receipt matches the digest and every file is there; with `model`, the
    stamped car must match too (a mismatch is fixed by a cheap re-stamp, not a
    re-render — see `render_job`)."""
    r = _receipt(job)
    if r is None:
        return False
    return r.get("stamp") == STAMP_VERSION and (model is None or r.get("model", "") == model)


def restamp_job(job: Job, rec: dict, *, model: str) -> dict:
    """Same media, new car name: rewrite the container metadata of every file in
    the receipt and the receipt itself (tags carry the model). No re-encode."""
    stamp(job, job.out_dir / rec["primary"].split("/", 1)[1], start_t=job.a, gps=True, model=model)
    for c in rec["children"]:
        stamp(job, job.out_dir / c.split("/", 1)[1], start_t=job.a, gps=True, model=model)
    rec = {**rec, "model": model, "stamp": STAMP_VERSION, "tags": tags_for(job, model),
           "restamped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (job.out_dir / "render.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def render_job(root: Path, job: Job, *, model: str, log: Callable[[str], None]) -> dict:
    fresh = _receipt(job)
    if fresh is not None and (fresh.get("model", "") != model or fresh.get("stamp") != STAMP_VERSION):
        log(f"    re-stamp  model {fresh.get('model', '') or '—'!r} → {model or '—'!r}, "
            f"stamp v{fresh.get('stamp', 1)} → v{STAMP_VERSION}")
        return restamp_job(job, fresh, model=model)
    job.out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="teslacam-", dir=str(job.folder)))
    try:
        mosaic = job.out_dir / "mosaic.mp4"
        log(f"    mosaic  [{job.a:.0f}–{job.b:.0f}s]")
        render_mosaic(root, job, mosaic, tmp, log)
        stamp(job, mosaic, start_t=job.a, gps=True, model=model)
        children: list[str] = []
        covered: dict[str, list[float]] = {}
        for cam in job.cams:
            out = job.out_dir / f"{cam}.mp4"
            log(f"    {cam}")
            try:
                sa, sb = render_child(root, job, cam, out, tmp)
            except NoFootage as e:
                log(f"    [yellow]skipped: {e}[/yellow]")
                out.unlink(missing_ok=True)
                continue
            # same CreationDate as the mosaic so the stack never splits across days
            stamp(job, out, start_t=job.a, gps=True, model=model)
            children.append(f"{job.slug}/{cam}.mp4")
            covered[cam] = [sa, sb]
        # stale outputs from a previous camera choice
        for p in job.out_dir.glob("*.mp4"):
            if p.name != "mosaic.mp4" and p.stem not in job.cams and p.stem in CAMERAS:
                p.unlink()
        frames = track_mod.frames_for_window(root, job.ev, job.snap_a, job.snap_b)
        has_track = track_mod.write_track(frames, job.out_dir, name=job.slug, event_id=job.ev.id)
        src_json = root / job.ev.dir / "event.json"
        if src_json.exists() and job.ev.kind != "recent":
            shutil.copyfile(src_json, job.out_dir / "event.json")
        rec = {"schema": 1, "version": RENDER_VERSION, "digest": job.digest, "key": job.ev.id,
               "event": job.ev.id, "slug": job.slug, "window": [job.a, job.b],
               "snapped": [job.snap_a, job.snap_b], "covered": covered, "precise": job.decision.precise,
               "primary": f"{job.slug}/mosaic.mp4", "children": children, "model": model, "stamp": STAMP_VERSION,
               "tags": tags_for(job, model), "archive": job.decision.archive, "track": has_track,
               "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (job.out_dir / "render.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        return rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
