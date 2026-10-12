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
        if s.cd:      # only events with a short camera: the others keep their old digest
            h.update(f"cd{sorted(s.cd.items())}".encode())
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
                 inpoint: float | None = None, outpoint: float | None = None) -> Segment | None:
    """Concat list of `cam`'s files over `segs`; returns the first segment that has
    one (where the list's time 0 sits, before `inpoint`), None when none does.

    Every file is declared to last until the next listed segment starts
    (`duration`), so a camera that stops early (2023 HW4 front: ~38 s of each
    minute) or a missing file leaves a timestamp gap instead of pulling the
    following footage forward. `inpoint` applies to the first listed file,
    `outpoint` to the last (seconds into that file)."""
    present = [s for s in segs if s.f.get(cam)]
    if not present:
        return None
    lines = []
    for i, s in enumerate(present):
        lines.append(f"file '{(root / ev.dir / s.f[cam]).as_posix()}'")
        span = (present[i + 1].start - s.start) if i + 1 < len(present) else s.d
        if i == 0 and inpoint is not None and inpoint > 0:
            lines.append(f"inpoint {inpoint:.3f}")
            span -= inpoint           # an explicit duration is not reduced by the in point
        lines.append(f"duration {span:.3f}")
        if i == len(present) - 1 and outpoint is not None:
            lines.append(f"outpoint {outpoint:.3f}")
    path.write_text("\n".join(lines) + "\n")
    return present[0]


def holes(ev: Event, cam: str, a: float, b: float) -> list[tuple[float, float]]:
    """Timeline intervals inside [a, b] where `cam` has no footage, relative to `a`:
    a missing file, or the tail of a file shorter than its segment."""
    out: list[tuple[float, float]] = []
    for s in ev.overlapping(a, b):
        start = s.start + (s.cam_d(cam) if s.f.get(cam) else 0.0)
        lo, hi = max(a, start), min(b, s.end)
        if hi - lo > 0.05:
            if out and abs(out[-1][1] - (lo - a)) < 1e-6:
                out[-1] = (out[-1][0], hi - a)
            else:
                out.append((lo - a, hi - a))
    return out


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
    font = _font()
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    n_in = 0
    for i, cam in enumerate(cams):
        lst = tmp / f"{cam}.txt"
        gaps = holes(ev, cam, job.a, job.b) if cam in ev.cams else []
        if sum(y - x for x, y in gaps) >= job.b - job.a - 0.1:
            first = None                 # only a hole here: a trim with no frames stalls xstack
        else:
            first = _concat_list(root, ev, cam, segs, lst) if cam in ev.cams else None
        if first is not None:
            inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
            # Shift by the window's in point, not by the first kept frame: after a
            # leading hole that frame is later than the window start.
            ta, tb = job.a - first.start, job.b - first.start
            chain = (f"[{n_in}:v]trim=start={max(0.0, ta):.3f}:end={tb:.3f},setpts=PTS-({ta:.3f}/TB),"
                     f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                     f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={MOSAIC_FPS}:start_time=0,"
                     # fps fills gaps between frames, not after the last one: carry the
                     # tile to the window end so a trailing hole gets masked too
                     f"tpad=stop_mode=clone:stop_duration={job.b - job.a:.3f}")
            n_in += 1
            if gaps:
                # fps fills a hole by repeating a frame; black it out and say so
                en = "+".join(f"between(t,{x:.3f},{y:.3f})" for x, y in gaps)
                chain += f",drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='{en}'"
                if font:
                    chain += (f",drawtext=fontfile='{font}':text='NO FOOTAGE':x=(w-tw)/2:y=(h-th)/2:"
                              f"fontsize=22:fontcolor=white@0.6:enable='{en}'")
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
    part = out
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", *inputs, "-filter_complex", fc, "-map", "[out]",
           "-an", *_h264_encoder(), "-pix_fmt", "yuv420p", "-t", f"{job.b - job.a:.3f}",
           "-movflags", "+faststart", "-tag:v", "avc1", "-f", "mp4", str(part)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg mosaic failed:\n" + r.stderr[-2000:])
    _verify_or_discard(part, job.b - job.a, tolerance=1.5)


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
    """Returns the (start, end) timeline seconds the file actually covers. A hole
    inside the window (camera stopped early, file missing) stays a gap in the file's
    timestamps; the file starts at the camera's first footage in the window and ends
    at its last."""
    ev = job.ev
    segs = [s for s in ev.overlapping(job.a, job.b) if cam in s.f]
    # footage of this camera that really lies inside the window
    segs = [s for s in segs if s.start + s.cam_d(cam) > job.a + 0.05]
    if not segs:
        raise NoFootage(f"{cam}: no files in the window")
    first, last = segs[0], segs[-1]
    ta = max(0.0, job.a - first.start)
    tb = min(job.b, last.end) - last.start
    tb_cam = min(tb, last.cam_d(cam))           # the window may end inside the hole
    if last.start + tb_cam <= first.start + ta + 0.05:
        raise NoFootage(f"{cam}: only a hole in the window")
    lst = tmp / f"child-{cam}.txt"
    srt = tmp / f"child-{cam}.srt"
    part = out
    if not job.decision.precise:
        # Lossless: the concat demuxer's inpoint/outpoint with stream copy. The cut
        # starts at the last keyframe before the in point (Tesla: every 0.5–0.75 s),
        # so the file covers [keyframe, out] and the wall clock is built from there.
        ka = snap_to_keyframe(root / ev.dir / first.f[cam], ta)
        sa, sb = first.start + ka, last.start + tb_cam
        _concat_list(root, ev, cam, segs, lst, inpoint=ka,
                     outpoint=tb_cam if tb_cam < last.cam_d(cam) - 0.05 else None)
        srt.write_text(_srt(ev, segs, sa, sb, ev.offset))
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-i", str(srt), "-map", "0:v", "-map", "1:s", "-c", "copy", "-c:s", "mov_text",
               "-avoid_negative_ts", "make_zero",
               "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Wall clock",
               "-disposition:s:0", "default", "-movflags", "+faststart", "-f", "mp4", str(part)]
    else:
        sa, sb = first.start + ta, last.start + tb_cam
        _concat_list(root, ev, cam, segs, lst)
        srt.write_text(_srt(ev, segs, sa, sb, ev.offset))
        br = str(segs[0].bitrate or 6_000_000)
        enc = ["-c:v", "h264_videotoolbox", "-b:v", br, "-profile:v", "high", "-allow_sw", "1"] \
            if "h264_videotoolbox" in " ".join(_h264_encoder()) else ["-c:v", "libx264", "-crf", "20"]
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
               "-i", str(srt), "-filter_complex",
               f"[0:v]trim=start={ta:.3f}:end={ta + (sb - sa):.3f},setpts=PTS-({ta:.3f}/TB)[v]",
               "-map", "[v]", "-map", "1:s", *enc, "-pix_fmt", "yuv420p", "-c:s", "mov_text",
               "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=Wall clock",
               "-movflags", "+faststart", "-f", "mp4", str(part)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg {cam} failed:\n" + r.stderr[-2000:])
    _verify_or_discard(part, sb - sa, tolerance=3.0)
    return sa, sb


def _verify_or_discard(part: Path, expected_s: float, *, tolerance: float) -> None:
    """`_verify`, removing the part file when it fails: a rejected render must not
    sit in the output folder, which may be inside Immich's library."""
    try:
        _verify(part, expected_s, tolerance=tolerance)
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _verify(path: Path, expected_s: float, *, tolerance: float) -> None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    j = json.loads(out.stdout or "{}")
    vids = [s for s in j.get("streams") or [] if s.get("codec_type") == "video"]
    if len(vids) != 1:
        raise RuntimeError(f"{path.name}: expected 1 video stream, found {len(vids)}")
    # the video's own length: a subtitle track can make the container look complete
    dur = float(vids[0].get("duration") or (j.get("format") or {}).get("duration") or 0)
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


# Files are rendered under a staged name and renamed into place only when the whole
# event succeeded. `._*` is in Immich's default library exclusions (macOS resource
# forks), so a render straight into the library never exposes a half-made event.
STAGE = "._"


def _staged(final: Path) -> Path:
    return final.with_name(STAGE + final.name)


def render_job(root: Path, job: Job, *, model: str, log: Callable[[str], None]) -> dict:
    fresh = _receipt(job)
    if fresh is not None and (fresh.get("model", "") != model or fresh.get("stamp") != STAMP_VERSION):
        log(f"    re-stamp  model {fresh.get('model', '') or '—'!r} → {model or '—'!r}, "
            f"stamp v{fresh.get('stamp', 1)} → v{STAMP_VERSION}")
        return restamp_job(job, fresh, model=model)
    job.out_dir.mkdir(parents=True, exist_ok=True)
    for p in [*job.out_dir.glob(f"{STAGE}*.mp4"), *job.out_dir.glob("*.part.mp4")]:
        p.unlink()                      # left by an interrupted run
    tmp = Path(tempfile.mkdtemp(prefix="teslacam-", dir=str(job.folder)))
    staged: dict[Path, Path] = {}       # staged file → final name
    try:
        mosaic = job.out_dir / "mosaic.mp4"
        log(f"    mosaic  [{job.a:.0f}–{job.b:.0f}s]")
        st = _staged(mosaic)
        staged[st] = mosaic
        render_mosaic(root, job, st, tmp, log)
        stamp(job, st, start_t=job.a, gps=True, model=model)
        children: list[str] = []
        covered: dict[str, list[float]] = {}
        skipped: list[Path] = []
        for cam in job.cams:
            out = job.out_dir / f"{cam}.mp4"
            log(f"    {cam}")
            st = _staged(out)
            try:
                sa, sb = render_child(root, job, cam, st, tmp)
            except NoFootage as e:
                log(f"    [yellow]skipped: {e}[/yellow]")
                st.unlink(missing_ok=True)
                skipped.append(out)
                continue
            staged[st] = out
            # same CreationDate as the mosaic so the stack never splits across days
            stamp(job, st, start_t=job.a, gps=True, model=model)
            children.append(f"{job.slug}/{cam}.mp4")
            covered[cam] = [sa, sb]
        frames = track_mod.frames_for_window(root, job.ev, job.snap_a, job.snap_b)
        # Side files first (Immich ignores them): a failure here publishes no media.
        has_track = track_mod.write_track(frames, job.out_dir, name=job.slug, event_id=job.ev.id)
        src_json = root / job.ev.dir / "event.json"
        if src_json.exists() and job.ev.kind != "recent":
            shutil.copyfile(src_json, job.out_dir / "event.json")
        # Every file rendered: publish them together (same names → Immich keeps the asset ids).
        for st, final in staged.items():
            os.replace(st, final)
        staged.clear()
        for p in skipped:
            p.unlink(missing_ok=True)
        # stale outputs from a previous camera choice
        for p in job.out_dir.glob("*.mp4"):
            if p.name != "mosaic.mp4" and p.stem not in job.cams and p.stem in CAMERAS:
                p.unlink()
        rec = {"schema": 1, "version": RENDER_VERSION, "digest": job.digest, "key": job.ev.id,
               "event": job.ev.id, "slug": job.slug, "window": [job.a, job.b],
               "snapped": [job.snap_a, job.snap_b], "covered": covered, "precise": job.decision.precise,
               "primary": f"{job.slug}/mosaic.mp4", "children": children, "model": model, "stamp": STAMP_VERSION,
               "tags": tags_for(job, model), "archive": job.decision.archive, "track": has_track,
               "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        # The receipt goes last and atomically: if anything above failed half-way,
        # there is no matching receipt and the next run renders the event again.
        tmp_rec = job.out_dir / "render.json.tmp"
        tmp_rec.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        os.replace(tmp_rec, job.out_dir / "render.json")
        return rec
    finally:
        for st in staged:              # a failed event publishes nothing
            st.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
