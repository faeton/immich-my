"""Re-date Google Takeout imports whose capture time came out wrong.

Two ways a Takeout file reached Immich with a bad date:

- **Placeholder.** The JSON companion wasn't found (Takeout moves a
  duplicate's `(n)` to the end of the JSON name; a Live Photo's video shares
  its still's JSON), so a fallback stamped the folder year: every such file
  sits at `YYYY-01-01 12:00:00`.
- **UTC.** The JSON was found, but its `photoTakenTime` (a UTC epoch) was
  written as `+00:00`, so Immich shows the right instant on a UTC clock,
  hours off and sometimes on the wrong calendar day.

For each asset this module finds its Takeout source through the dedup
manifest (promote placed every file at a path derived from its staging
path and date, so the link is exact, not a name guess), reads the JSON
(`dedup.engine._google_json_companion`, which knows Takeout's naming), and
works out the local clock:

    zone  ← the file's own GPS, else the JSON's geoData (offline
            timezonefinder), else the zone most assets shot within a few
            hours of that instant carry, else UTC.

A file with no JSON at all is dated from its numbered neighbours in the same
Takeout folder (IMG_0211 / IMG_0213 for IMG_0212), only when both sides
exist and agree to within two days.

The fix goes where Immich reads it: `DateTimeOriginal` with its offset in
the asset's XMP sidecar, the sidecar registered on the asset (`asset_file`,
what Immich's SidecarCheck job does), then a per-asset metadata refresh.
Originals are never touched. Every change is logged for undo.

`stack_twins` then stacks a Takeout copy onto the library original it
duplicates (Google re-encodes, so the bytes differ). The original stays the
primary.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .dedup.engine import _google_json_companion, _promote_dest, xmp_datetime, zone_at


# --- linking Immich assets to Takeout sources --------------------------------


def manifest_index(rows) -> dict[str, set[str]]:
    """Library-relative path → staging paths that promote could have put
    there. `rows` are (manifest id, staging path, taken_at, old_taken_at):
    the date at promote time decided the `YYYY/MM` folder, and a later date
    fix may have changed `taken_at` since, so both are tried, each with the
    `__<id>` collision name too."""
    index: dict[str, set[str]] = {}
    root = Path("/")
    for mid, path, taken_at, old_taken_at in rows:
        for ta in {taken_at, old_taken_at} - {None}:
            dest = _promote_dest(root, path, ta)
            for d in (dest, dest.with_name(f"{dest.stem}__{mid}{dest.suffix}")):
                index.setdefault(str(d.relative_to(root)), set()).add(path)
    return index


# --- dates -------------------------------------------------------------------


@dataclass(frozen=True)
class Taken:
    instant: datetime        # aware, UTC
    source: str              # "json" | "neighbour-file"
    lat: float | None = None
    lon: float | None = None


def json_taken(media: Path) -> Taken | None:
    data = _google_json_companion(media)
    if not data:
        return None
    ts = (data.get("photoTakenTime") or {}).get("timestamp")
    if not ts:
        return None
    geo = data.get("geoData") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    if not lat or not lon or (abs(lat) < 1e-3 and abs(lon) < 1e-3):
        lat = lon = None
    return Taken(datetime.fromtimestamp(int(ts), timezone.utc), "json", lat, lon)


def file_taken(library_file: Path) -> Taken | None:
    """The file's own embedded capture time, read exactly as ingest reads it
    (`capture.capture_time`: offsets honoured, QuickTime CreateDate as UTC).
    Some Takeout files that lost their JSON still carry it; a recorded
    instant from the file beats any inference from neighbours. Only an
    absolute time counts. A bare wall clock with no zone is not an instant."""
    if not library_file.is_file():
        return None
    import json
    import subprocess
    from .capture import capture_time
    from .exif import ExifRow
    out = subprocess.run(
        ["exiftool", "-j", "-G", "-n", "-fast", "-m", str(library_file)],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return None
    raw = json.loads(out.stdout)[0]
    ct = capture_time(ExifRow(path=library_file, raw=raw))
    if ct is None or not ct.absolute or ct.instant.year < 1995:
        return None
    return Taken(ct.instant.astimezone(timezone.utc), "file")


_NUMBERED = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)(?:\(\d+\))?$")


def neighbour_taken(media: Path, *, reach: int = 5, agree: timedelta = timedelta(days=2)) -> Taken | None:
    """Date a JSON-less `IMG_0212(1).mp4` from IMG_0211 / IMG_0213 in the same
    Takeout folder: a camera counter is monotonic in time, so the shot sits
    between its numbered neighbours. Needs a neighbour on both sides within
    `reach` numbers whose dates agree to within `agree`, because another
    device's IMG_0213 from the same year would otherwise be trusted. The
    result is interpolated by counter position."""
    m = _NUMBERED.match(Path(media.name).stem)
    if not m:
        return None
    prefix, num, width = m.group("prefix"), int(m.group("num")), len(m.group("num"))
    ext = media.suffix

    # Same extension, same case only: one Takeout year folder mixes several
    # phones' IMG_#### ranges, and the extension (`.mp4` vs `.MOV` vs
    # `.HEIC`) is the best same-device signal a name carries.
    def at(n: int) -> datetime | None:
        t = json_taken(media.with_name(f"{prefix}{n:0{width}d}{ext}"))
        return t.instant if t else None

    below = next(((num - k, t) for k in range(1, reach + 1) if (t := at(num - k))), None)
    above = next(((num + k, t) for k in range(1, reach + 1) if (t := at(num + k))), None)
    if not below or not above:
        return None
    (n0, t0), (n1, t1) = below, above
    if not timedelta(0) <= t1 - t0 <= agree:
        return None
    frac = (num - n0) / (n1 - n0)
    return Taken(t0 + (t1 - t0) * frac, "neighbour-file")


# --- zones -------------------------------------------------------------------

_IMMICH_OFFSET = re.compile(r"^UTC(?P<sign>[+-])(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?$")


def parse_zone(name: str | None):
    """Immich's `timeZone`: an IANA name (`Europe/Lisbon`) or a fixed offset
    in its own spelling (`UTC+2`, `UTC-03:30`). UTC itself → None, since
    that is exactly the answer this module is trying to improve on."""
    if not name or name in ("UTC", "UTC+0", "Etc/UTC", "UTC+00:00"):
        return None
    m = _IMMICH_OFFSET.match(name)
    if m:
        mins = int(m.group("h")) * 60 + int(m.group("m") or 0)
        mins = -mins if m.group("sign") == "-" else mins
        return timezone(timedelta(minutes=mins)) if mins else None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def zone_label(zone) -> str:
    return getattr(zone, "key", None) or str(zone)


# --- the plan ----------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    asset_id: str
    rel: str                       # path under the library import path
    local_date: datetime           # Immich's current wall clock
    file_lat: float | None
    file_lon: float | None
    reason: str                    # "placeholder" | "utc"


@dataclass
class Fix:
    target: Target
    staging: str | None = None
    taken: Taken | None = None
    zone: object = None
    zone_source: str = ""
    problem: str | None = None

    @property
    def local(self) -> datetime | None:
        if not self.taken:
            return None
        return self.taken.instant.astimezone(self.zone) if self.zone else self.taken.instant

    @property
    def xmp(self) -> str | None:
        return xmp_datetime(self.local) if self.local else None


_FOLDER_YEAR = re.compile(r"Photos from (\d{4})$")


def consistent(target: Target, staging: str, taken: Taken) -> str | None:
    """Why this source can't be the right one for `target`, or None.

    - placeholder: the stamp's year *is* the Takeout folder year it was
      derived from, so the source must sit in `Photos from <that year>`.
    - utc: Immich must currently show exactly Google's instant on a UTC
      clock. Anything else means the file carries its own date (camera EXIF,
      already local, or a camera clock that disagrees with Google). That
      date is left alone: Google's is not better evidence than the file's.
    """
    if target.reason == "placeholder":
        m = _FOLDER_YEAR.search(Path(staging).parent.name)
        if m and int(m.group(1)) != target.local_date.year:
            return "folder year differs"
        return None
    shown = target.local_date.replace(tzinfo=timezone.utc)
    if taken.source == "neighbour-file" or abs((shown - taken.instant).total_seconds()) > 2:
        return "own date disagrees with takeout"
    return None


def plan(
    targets: list[Target],
    index: dict[str, set[str]],
    *,
    takeout_root: Path,
    staging_prefix: str,
    neighbour_zone,
    library_root: Path | None = None,
) -> list[Fix]:
    """One `Fix` per target; `problem` says why one can't be fixed.
    `neighbour_zone(instant, asset_id)` returns the zone name nearby shots
    carry, or None. Several possible sources are narrowed by `consistent`;
    exactly one must survive.

    Date authority: the Takeout JSON → the library file's own embedded
    capture time (`library_root` / rel) → numbered neighbours."""
    fixes: list[Fix] = []
    for t in targets:
        fix = Fix(target=t)
        fixes.append(fix)
        stagings = sorted(index.get(t.rel, set()))
        if not stagings:
            fix.problem = "no takeout source"
            continue
        options: list[tuple[str, Taken]] = []
        reasons: list[str] = []
        for staging in stagings:
            try:
                media = takeout_root / Path(staging).relative_to(staging_prefix)
            except ValueError:
                reasons.append(f"source outside {staging_prefix}")
                continue
            taken = (json_taken(media)
                     or (file_taken(library_root / t.rel) if library_root else None)
                     or neighbour_taken(media))
            if not taken:
                reasons.append("no json, no datable neighbours")
                continue
            why = consistent(t, staging, taken)
            if why:
                reasons.append(why)
                continue
            options.append((staging, taken))
        # The same photo exported into two Takeout folders: sources that
        # agree on the instant are one answer.
        if len(options) > 1 and all(
            abs((o[1].instant - options[0][1].instant).total_seconds()) <= 2 for o in options
        ):
            options = options[:1]
        if len(options) != 1:
            fix.problem = ("several takeout sources" if options
                           else (reasons[0] if len(set(reasons)) == 1 else "; ".join(sorted(set(reasons)))))
            continue
        fix.staging, fix.taken = options[0]
        for src, lat, lon in (("file gps", t.file_lat, t.file_lon),
                              ("json gps", fix.taken.lat, fix.taken.lon)):
            zone = zone_at(lat, lon)
            if zone is not None:
                fix.zone, fix.zone_source = zone, src
                break
        else:
            zone = parse_zone(neighbour_zone(fix.taken.instant, t.asset_id))
            fix.zone, fix.zone_source = (zone, "nearby shots") if zone else (None, "utc")
        if t.reason == "utc" and fix.zone is None:
            fix.problem = "still no zone"  # nothing to improve on
    return fixes


# --- twins -------------------------------------------------------------------

_COPY_SUFFIX = re.compile(r"(?:\(\d+\))?(?:__\d+)?(?P<ext>\.[^.]+)$")


def original_name(name: str) -> str:
    """`IMG_1711(1).MP4` / `IMG_1711__66958.MP4` → `IMG_1711.MP4`."""
    return _COPY_SUFFIX.sub(r"\g<ext>", name)


@dataclass(frozen=True)
class Candidate:
    asset_id: str
    created: datetime     # Immich fileCreatedAt (aware)
    stack_id: str | None
    clip_dist: float | None


def is_twin(instant: datetime, c: Candidate) -> bool:
    """Same shot: the same instant to the second, or the same minutes and
    seconds a whole number of hours apart (the original was zoned wrongly,
    like a UTC-stamped rescue). A CLIP embedding, when both have one, must
    agree; it vetoes a different photo that happens to share a name."""
    if c.clip_dist is not None and c.clip_dist >= 0.15:
        return False
    delta = abs((c.created - instant).total_seconds())
    if delta <= 2:
        return True
    hours, rest = divmod(delta, 3600)
    return hours <= 14 and (rest <= 2 or rest >= 3598)


def pick_twin(instant: datetime, candidates: list[Candidate]) -> Candidate | None:
    twins = [c for c in candidates if is_twin(instant, c)]
    return twins[0] if len(twins) == 1 else None


__all__ = [
    "manifest_index", "Taken", "json_taken", "file_taken", "neighbour_taken",
    "parse_zone", "zone_label", "Target", "Fix", "consistent", "plan",
    "original_name", "Candidate", "is_twin", "pick_twin",
]
