#!/usr/bin/env python3
"""
Throwaway pre-immy audit. Read-only. Run on real trip folders to see the mess
before we commit to rule priorities in immy proper.

Usage:
    tools/audit.py ~/Documents/Incoming/Mau-Lions-1 [...more folders]

Requires exiftool on PATH (brew install exiftool).
Not precious code — will be superseded by immy.
"""
from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# --- config ---------------------------------------------------------------

MEDIA_EXT = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".webp",
    ".raf", ".arw", ".cr2", ".cr3", ".nef", ".dng", ".orf", ".rw2",
    ".mp4", ".mov", ".m4v", ".avi", ".mts", ".mkv",
    ".insv", ".insp", ".lrv", ".lrf",
    ".braw", ".mxf",
}
CAMERA_NATIVE_PREFIXES = {
    "DJI_": "DJI",
    "GX": "GoPro",
    "GH": "GoPro",
    "GOPR": "GoPro",
    "DSC_": "Sony/Nikon",
    "MVI_": "Canon",
    "MAH": "Panasonic",
    "VID_": "generic",
    "IMG_": "generic",
    "C0": "Sony",
    "LRV_": "Insta360",
    "PRO_": "Insta360",
}
SIDECAR_EXT = {".srt", ".lrv", ".lrf", ".xmp", ".thm"}

# --- exiftool glue --------------------------------------------------------

EXIFTOOL_TAGS = [
    "-SourceFile",
    "-FileName",
    "-Directory",
    "-FileSize#",
    "-Make",
    "-Model",
    "-DateTimeOriginal",
    "-CreateDate",
    "-ModifyDate",
    "-OffsetTimeOriginal",
    "-OffsetTime",
    "-GPSLatitude#",
    "-GPSLongitude#",
    "-GPSAltitude#",
    "-ImageWidth",
    "-ImageHeight",
    "-Duration#",
    "-MIMEType",
    "-VideoFrameRate",
    "-CompressorID",
    "-AvgBitrate#",
    "-CameraModelName",
]


def run_exiftool(folder: Path) -> list[dict]:
    cmd = [
        "exiftool",
        "-j",
        "-r",
        "-fast2",
        "-n",  # numeric GPS
        "-q",
        "-ext", "*",
        "--ext", "DS_Store",
        "--ext", "aae",
        *EXIFTOOL_TAGS,
        str(folder),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if not out.stdout.strip():
        return []
    return json.loads(out.stdout)


# --- parsing helpers ------------------------------------------------------

DATE_RE = re.compile(
    r"^(?P<y>\d{4})[:\-](?P<m>\d{2})[:\-](?P<d>\d{2})"
    r"[ T](?P<H>\d{2}):(?P<M>\d{2}):(?P<S>\d{2})"
)


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    m = DATE_RE.match(s)
    if not m:
        return None
    try:
        return datetime(
            int(m["y"]), int(m["m"]), int(m["d"]),
            int(m["H"]), int(m["M"]), int(m["S"]),
        )
    except ValueError:
        return None


def filename_prefix(name: str) -> str | None:
    for pref in CAMERA_NATIVE_PREFIXES:
        if name.startswith(pref):
            return pref
    return None


# --- record ---------------------------------------------------------------

@dataclass
class Rec:
    path: Path
    ext: str
    size: int
    make: str | None
    model: str | None
    dto: datetime | None       # DateTimeOriginal
    cdate: datetime | None     # CreateDate
    mdate: datetime | None     # ModifyDate
    tz: str | None
    gps: tuple[float, float] | None
    width: int | None
    height: int | None
    duration: float | None
    mime: str | None
    bitrate: int | None
    name_prefix: str | None

    @property
    def camera_key(self) -> str:
        if self.make and self.model:
            return f"{self.make} {self.model}".strip()
        if self.name_prefix:
            return f"[prefix] {self.name_prefix}"
        return "[unknown]"

    @property
    def capture_dt(self) -> datetime | None:
        return self.dto or self.cdate


def ingest(rows: list[dict], root: Path) -> list[Rec]:
    out = []
    for r in rows:
        sf = Path(r.get("SourceFile", ""))
        ext = sf.suffix.lower()
        if ext not in MEDIA_EXT and ext not in SIDECAR_EXT:
            continue
        gps_lat = r.get("GPSLatitude")
        gps_lon = r.get("GPSLongitude")
        gps = (float(gps_lat), float(gps_lon)) if gps_lat is not None and gps_lon is not None else None
        name = sf.name
        out.append(Rec(
            path=sf,
            ext=ext,
            size=int(r.get("FileSize") or 0),
            make=(r.get("Make") or "").strip() or None,
            model=(r.get("Model") or r.get("CameraModelName") or "").strip() or None,
            dto=parse_dt(r.get("DateTimeOriginal")),
            cdate=parse_dt(r.get("CreateDate")),
            mdate=parse_dt(r.get("ModifyDate")),
            tz=r.get("OffsetTimeOriginal") or r.get("OffsetTime"),
            gps=gps,
            width=r.get("ImageWidth"),
            height=r.get("ImageHeight"),
            duration=r.get("Duration"),
            mime=r.get("MIMEType"),
            bitrate=r.get("AvgBitrate"),
            name_prefix=filename_prefix(name),
        ))
    return out


# --- pivot + analysis -----------------------------------------------------

@dataclass
class CamStats:
    name: str
    recs: list[Rec] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.recs)

    def dates(self) -> list[datetime]:
        return [r.capture_dt for r in self.recs if r.capture_dt]

    def date_range(self) -> tuple[datetime, datetime] | None:
        ds = self.dates()
        if not ds:
            return None
        return min(ds), max(ds)

    def missing_date(self) -> int:
        return sum(1 for r in self.recs if not r.capture_dt)

    def missing_gps(self) -> int:
        # only meaningful for media, not XMP/SRT
        media = [r for r in self.recs if r.ext in MEDIA_EXT]
        if not media:
            return 0
        return sum(1 for r in media if r.gps is None)

    def missing_tz(self) -> int:
        return sum(1 for r in self.recs if r.capture_dt and not r.tz)

    def export_trap(self) -> int:
        hits = 0
        for r in self.recs:
            if r.dto and r.mdate and (r.mdate - r.dto) > timedelta(days=30):
                hits += 1
        return hits

    def bytes(self) -> int:
        return sum(r.size for r in self.recs)


def pivot_by_camera(recs: list[Rec]) -> dict[str, CamStats]:
    cams: dict[str, CamStats] = {}
    for r in recs:
        k = r.camera_key
        if k not in cams:
            cams[k] = CamStats(name=k)
        cams[k].recs.append(r)
    return cams


def median_offset_vs_reference(
    target: list[Rec], reference: list[Rec], window: timedelta = timedelta(hours=1)
) -> tuple[timedelta | None, int]:
    """
    For each target capture, find the closest reference capture within `window`.
    Return the median signed offset (target - reference) and the number of pairs.
    """
    ref_ts = sorted(r.capture_dt for r in reference if r.capture_dt)
    if not ref_ts:
        return None, 0
    deltas = []
    for t in target:
        if not t.capture_dt:
            continue
        # binary-search-ish with a naive min for simplicity
        closest = min(ref_ts, key=lambda r: abs(r - t.capture_dt))
        d = t.capture_dt - closest
        if abs(d) < timedelta(days=2):  # bound sanity
            deltas.append(d)
    if not deltas:
        return None, 0
    seconds = sorted(d.total_seconds() for d in deltas)
    med = seconds[len(seconds) // 2]
    return timedelta(seconds=med), len(deltas)


def find_pairs(recs: list[Rec]) -> dict[str, int]:
    """Count files whose stems also exist with a sidecar extension."""
    stems: dict[str, set[str]] = defaultdict(set)
    for r in recs:
        stems[r.path.stem].add(r.ext)
    pairs = {
        "dji_srt": 0,       # .MP4/.MOV + .SRT
        "insta360_lrv": 0,  # .insv + .lrv
        "xmp_orphan": 0,    # .xmp without a paired media
    }
    for stem, exts in stems.items():
        if ".srt" in exts and (exts & {".mp4", ".mov"}):
            pairs["dji_srt"] += 1
        if ".insv" in exts and ".lrv" in exts:
            pairs["insta360_lrv"] += 1
        if ".xmp" in exts and not (exts - {".xmp"}):
            pairs["xmp_orphan"] += 1
    return pairs


# --- printing -------------------------------------------------------------

def fmt_bytes(n: int) -> str:
    for unit, step in [("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]:
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} B"


def fmt_delta(d: timedelta) -> str:
    total = int(d.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}"


def report_folder(folder: Path) -> None:
    print("=" * 78)
    print(f"TRIP  {folder}")
    print("=" * 78)
    rows = run_exiftool(folder)
    recs = ingest(rows, folder)
    if not recs:
        print("  (no media files found)")
        return

    # top-level summary
    total_bytes = sum(r.size for r in recs)
    exts = Counter(r.ext for r in recs)
    print(f"  files: {len(recs)}   size: {fmt_bytes(total_bytes)}")
    print(f"  ext:   {dict(exts.most_common())}")
    print()

    # per-camera pivot
    cams = pivot_by_camera(recs)
    name_w = max((len(k) for k in cams), default=10)
    hdr = f"  {'camera':<{name_w}}  {'n':>6}  {'size':>9}  {'first':<16}  {'last':<16}  {'no-dt':>6}  {'no-gps':>7}  {'no-tz':>6}  {'exp?':>5}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    # sort by byte weight desc
    for k in sorted(cams, key=lambda x: -cams[x].bytes()):
        c = cams[k]
        dr = c.date_range()
        first = dr[0].strftime("%Y-%m-%d %H:%M") if dr else "—"
        last = dr[1].strftime("%Y-%m-%d %H:%M") if dr else "—"
        print(
            f"  {k:<{name_w}}  {c.n:>6}  {fmt_bytes(c.bytes()):>9}  "
            f"{first:<16}  {last:<16}  "
            f"{c.missing_date():>6}  {c.missing_gps():>7}  {c.missing_tz():>6}  "
            f"{c.export_trap():>5}"
        )
    print()

    # cross-camera offsets (pick iphone-ish as reference)
    ref_key = None
    for k in cams:
        kl = k.lower()
        if "iphone" in kl or "apple" in kl:
            ref_key = k
            break
    if not ref_key:
        # fall back to biggest n
        ref_key = max(cams, key=lambda x: cams[x].n)

    print(f"  clock-offset vs reference [{ref_key}]:")
    any_drift = False
    for k, c in sorted(cams.items()):
        if k == ref_key:
            continue
        off, pairs = median_offset_vs_reference(c.recs, cams[ref_key].recs)
        if off is None or pairs == 0:
            print(f"    {k:<{name_w}}  (no overlap)")
            continue
        flag = "  ⚠" if abs(off) > timedelta(minutes=10) else ""
        if abs(off) > timedelta(minutes=10):
            any_drift = True
        print(f"    {k:<{name_w}}  median {fmt_delta(off):>12}  over {pairs:>4} pairs{flag}")
    if not any_drift:
        print("    (all cameras within ±10min of reference)")
    print()

    # pairs & flags
    pairs = find_pairs(recs)
    print("  pair signals:")
    print(f"    DJI MP4↔SRT pairs:     {pairs['dji_srt']}")
    print(f"    Insta360 .insv↔.lrv:   {pairs['insta360_lrv']}")
    print(f"    orphan .xmp sidecars:  {pairs['xmp_orphan']}")
    print()

    # filename-prefix breakdown (camera-native signal)
    prefix_counts = Counter(r.name_prefix for r in recs if r.name_prefix)
    if prefix_counts:
        print("  camera-native filename prefixes:")
        for p, n in prefix_counts.most_common():
            print(f"    {p:<8} {n:>5}   ({CAMERA_NATIVE_PREFIXES.get(p, '?')})")
    print()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: audit.py <folder> [<folder> ...]", file=sys.stderr)
        return 2
    for a in argv:
        p = Path(a).expanduser().resolve()
        if not p.is_dir():
            print(f"not a directory: {p}", file=sys.stderr)
            continue
        report_folder(p)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
