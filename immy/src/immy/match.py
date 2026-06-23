"""`immy match` — place an inbound media dump against the existing library.

Answers, for a folder of about-to-be-imported media: *is this already in
Immich, and which trip does it belong to?* — fully offline, from a v2
snapshot (see `snapshot.py`). Read-only: it proposes, it never writes.

Three signals, folded into one report:

1. **Dedup** — every inbound file is checked against the snapshot by
   `(filename, size)` and (on a match) SHA1, reusing the `find-duplicates`
   classifier. Already-imported files are flagged and excluded from
   trip placement.

2. **Existing trips** — reconstructed two ways and used together:
   - real **immy-cluster albums** (recomputing date range + centroid +
     radius from their member assets), keeping the album's name;
   - **raw points**: assets in no album are re-clustered with the same
     time+space sweep `immy cluster` uses, to synthesise implicit trips.

3. **Placement** — each inbound event/file is matched against the nearest
   trip by haversine ≤ radius+max_km *and* date overlap (± max_gap):
   `matched` / `extends` / `new`. Drone & video clips frequently carry no
   EXIF GPS (it lives in the `.SRT` — a documented fast-follow), so those
   fall back to a **date-only** placement, clearly labelled lower-confidence.

Two groupings are reported side by side: per top-level **subfolder**
(with a flag when one folder spans multiple trips) and per self-clustered
**event**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import clustering
from .clustering import AssetPoint, haversine_km

# A point further than this from every trip is its own new trip; within it
# (but outside a trip's radius+max_km) it reads as extending a nearby trip.
DEFAULT_EXTEND_KM = 50.0


# --- value types ----------------------------------------------------------


@dataclass(frozen=True)
class ExistingTrip:
    """A trip reconstructed from the snapshot — an album or a raw-point
    cluster. `lat`/`lon` are the member centroid (None if no member had
    coords); `radius_km` is the max member distance from it."""

    name: str
    source: str  # "album" | "cluster"
    start: datetime | None
    end: datetime | None
    lat: float | None
    lon: float | None
    radius_km: float
    asset_count: int


@dataclass(frozen=True)
class InboundItem:
    """One inbound media file. `dup_kind` is set when the snapshot already
    has it (`exact` = SHA1, `likely` = name+size, `name-only` = name, size
    differs)."""

    path: Path
    subfolder: str  # top-level dir under the inbound root ("" if at root)
    size: int
    when: datetime | None
    lat: float | None
    lon: float | None
    asset_type: str  # "IMAGE" | "VIDEO" | "OTHER"
    dup_kind: str | None = None

    @property
    def is_dup(self) -> bool:
        return self.dup_kind in ("exact", "likely")


@dataclass(frozen=True)
class Placement:
    """Where an item/event landed against the existing library."""

    verdict: str  # "duplicate" | "matched" | "extends" | "new"
    trip: ExistingTrip | None
    confidence: str  # "geo" | "date-only" | "none"
    distance_km: float | None
    reason: str


@dataclass
class FolderReport:
    subfolder: str
    total: int
    duplicates: int
    placements: dict[str, int] = field(default_factory=dict)  # verdict -> n
    trips: set[str] = field(default_factory=set)  # distinct existing trips hit
    new_events: int = 0

    @property
    def spans_multiple(self) -> bool:
        return len(self.trips) > 1


@dataclass
class EventReport:
    when_range: tuple[datetime, datetime]
    size: int
    centroid: tuple[float, float] | None
    placement: Placement


@dataclass
class MatchReport:
    total_files: int
    duplicates: int
    folders: list[FolderReport]
    events: list[EventReport]
    gps_less: int  # non-dup items with no coords (date-only placement)


# --- helpers --------------------------------------------------------------


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Normalise to naive-UTC so aware (DB) and naive (EXIF) datetimes
    compare. Coarse but trip placement only needs day-scale accuracy."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _naive_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


# --- existing trips -------------------------------------------------------


def build_existing_trips(
    assets,            # list[snapshot.AssetRow]
    albums,            # list[snapshot.AlbumRow]
    membership: dict[str, set[str]],
    *,
    max_gap_hours: float = clustering.DEFAULT_MAX_GAP_HOURS,
    max_km: float = clustering.DEFAULT_MAX_KM,
    min_assets: int = clustering.DEFAULT_MIN_ASSETS,
) -> list[ExistingTrip]:
    """Albums (with recomputed bounds) + raw-point clusters over the rest."""
    by_id = {a.asset_id: a for a in assets}
    trips: list[ExistingTrip] = []
    claimed: set[str] = set()

    for album in albums:
        members = [by_id[i] for i in membership.get(album.album_id, ()) if i in by_id]
        if not members:
            continue
        trip = _trip_from_members(album.name, "album", members)
        # An album whose members all lack a capture date yields no usable
        # bounds — `place()` would skip it. Don't claim those members; leave
        # them for raw-point clustering (Grok review).
        if trip.start is None or trip.end is None:
            continue
        claimed.update(a.asset_id for a in members)
        trips.append(trip)

    # Raw points: everything not in a marker album, with date + coords.
    points = [
        AssetPoint(
            asset_id=a.asset_id, when=when, lat=a.lat, lon=a.lon,
            city=a.city, country=a.country,
        )
        for a in assets
        if a.asset_id not in claimed
        and a.lat is not None and a.lon is not None
        and (when := _parse_dt(a.taken_at)) is not None
    ]
    for cluster in clustering.cluster_assets(
        points, max_gap_hours=max_gap_hours, max_km=max_km, min_assets=min_assets,
    ):
        cen_lat, cen_lon = cluster.centroid
        radius = max(
            (haversine_km(cen_lat, cen_lon, p.lat, p.lon) for p in cluster.assets),
            default=0.0,
        )
        trips.append(ExistingTrip(
            name=cluster.name(), source="cluster",
            start=cluster.start, end=cluster.end,
            lat=cen_lat, lon=cen_lon, radius_km=radius,
            asset_count=len(cluster.assets),
        ))
    return trips


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _percentile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    return sorted_xs[min(len(sorted_xs) - 1, int(q * len(sorted_xs)))]


def _robust_date_bounds(dates: list[datetime]) -> tuple[datetime, datetime]:
    """min/max after IQR-fencing out outliers. A user album can hold a few
    misdated assets (one 2016 frame among a 2025 trip blows a raw min/max to
    a 9-year span that then date-matches everything), so fence them out."""
    ds = sorted(dates)
    n = len(ds)
    if n < 4:
        return ds[0], ds[-1]
    ep = [d.timestamp() for d in ds]
    q1, q3 = ep[n // 4], ep[(3 * n) // 4]
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    keep = [d for d, e in zip(ds, ep) if lo <= e <= hi]
    return (keep[0], keep[-1]) if keep else (ds[0], ds[-1])


def _trip_from_members(name: str, source: str, members) -> ExistingTrip:
    dates = [d for a in members if (d := _parse_dt(a.taken_at)) is not None]
    coords = [(a.lat, a.lon) for a in members if a.lat is not None and a.lon is not None]
    start = end = None
    if dates:
        start, end = _robust_date_bounds(dates)
    lat = lon = None
    radius = 0.0
    if coords:
        # Median centroid + 90th-percentile radius — both robust to a stray
        # mislocated asset (which would otherwise drag the mean + inflate a
        # max-distance radius across the planet).
        lat = _median([c[0] for c in coords])
        lon = _median([c[1] for c in coords])
        dists = sorted(haversine_km(lat, lon, c[0], c[1]) for c in coords)
        radius = _percentile(dists, 0.9)
    return ExistingTrip(
        name=name, source=source, start=start, end=end,
        lat=lat, lon=lon, radius_km=radius, asset_count=len(members),
    )


# --- placement ------------------------------------------------------------


def place(
    when: datetime | None,
    lat: float | None,
    lon: float | None,
    trips: list[ExistingTrip],
    *,
    max_km: float = clustering.DEFAULT_MAX_KM,
    max_gap_hours: float = clustering.DEFAULT_MAX_GAP_HOURS,
    extend_km: float = DEFAULT_EXTEND_KM,
) -> Placement:
    """Place a single (when, lat, lon) against the existing trips.

    Precedence (best wins): geo-matched > date-only-matched > geo-extends >
    date-only-extends > new. `matched` means within the trip's spatial
    extent AND its date range; `extends` means date-adjacent (within the
    gap) or just outside the radius but nearby.
    """
    when = _naive_utc(when)
    if when is None:
        return Placement("new", None, "none", None, "no capture date")
    gap = timedelta(hours=max_gap_hours)
    has_geo = lat is not None and lon is not None
    # candidate tuple: (rank, distance_sort, trip, verdict, confidence, dist)
    best: tuple | None = None
    for t in trips:
        start, end = _naive_utc(t.start), _naive_utc(t.end)
        if start is None or end is None:
            continue
        in_core = start <= when <= end
        in_slack = (start - gap) <= when <= (end + gap)
        if not in_slack:
            continue
        if has_geo and t.lat is not None and t.lon is not None:
            d = haversine_km(lat, lon, t.lat, t.lon)
            if d <= t.radius_km + max_km:
                rank, verdict = (0, "matched") if in_core else (2, "extends")
                cand = (rank, d, t, verdict, "geo", d)
            elif d <= extend_km:
                cand = (2, d, t, "extends", "geo", d)
            else:
                continue
        else:
            rank, verdict = (1, "matched") if in_core else (3, "extends")
            cand = (rank, 0.0, t, verdict, "date-only", None)
        if best is None or cand[:2] < best[:2]:
            best = cand
    if best is None:
        return Placement("new", None, "geo" if has_geo else "date-only", None,
                         "no trip within date+distance window")
    _, _, t, verdict, confidence, dist = best
    reason = (
        f"{verdict} {t.name}"
        + (f" ({dist:.1f} km)" if dist is not None else " (date window)")
    )
    return Placement(verdict, t, confidence, dist, reason)


# --- report (pure) --------------------------------------------------------


def build_report(
    items: list[InboundItem],
    trips: list[ExistingTrip],
    *,
    max_km: float = clustering.DEFAULT_MAX_KM,
    max_gap_hours: float = clustering.DEFAULT_MAX_GAP_HOURS,
    min_event_assets: int = clustering.DEFAULT_MIN_ASSETS,
) -> MatchReport:
    """Fold dedup + per-folder + per-event groupings into one report."""
    dup_total = sum(1 for it in items if it.is_dup)
    live = [it for it in items if not it.is_dup]

    # --- grouping A: per top-level subfolder ---
    folders: dict[str, FolderReport] = {}
    for it in items:
        fr = folders.setdefault(it.subfolder, FolderReport(it.subfolder, 0, 0))
        fr.total += 1
        if it.is_dup:
            fr.duplicates += 1
            continue
        p = place(it.when, it.lat, it.lon, trips,
                  max_km=max_km, max_gap_hours=max_gap_hours)
        fr.placements[p.verdict] = fr.placements.get(p.verdict, 0) + 1
        if p.trip is not None:
            fr.trips.add(p.trip.name)
        elif p.verdict == "new":
            fr.new_events += 1

    # --- grouping B: self-clustered events (geo items only) ---
    geo_points = [
        AssetPoint(asset_id=str(i), when=it.when, lat=it.lat, lon=it.lon,
                   city=None, country=None)
        for i, it in enumerate(live)
        if it.when is not None and it.lat is not None and it.lon is not None
    ]
    events: list[EventReport] = []
    for cluster in clustering.cluster_assets(
        geo_points, max_km=max_km, max_gap_hours=max_gap_hours,
        min_assets=min_event_assets,
    ):
        cen_lat, cen_lon = cluster.centroid
        # Place a multi-day event at its date midpoint, not its start, so a
        # dump spanning the edge of a trip's range still matches (Grok review).
        mid = cluster.start + (cluster.end - cluster.start) / 2
        events.append(EventReport(
            when_range=(cluster.start, cluster.end),
            size=len(cluster.assets),
            centroid=(cen_lat, cen_lon),
            placement=place(mid, cen_lat, cen_lon, trips,
                            max_km=max_km, max_gap_hours=max_gap_hours),
        ))

    # "GPS-less" = has a date but no coordinates (→ date-only placement).
    # Dateless items are a separate case (placed as `new`, reason "no date").
    gps_less = sum(
        1 for it in live
        if it.when is not None and (it.lat is None or it.lon is None)
    )
    return MatchReport(
        total_files=len(items), duplicates=dup_total,
        folders=sorted(folders.values(), key=lambda f: f.subfolder),
        events=events, gps_less=gps_less,
    )


# --- inbound scan (IO boundary) -------------------------------------------


_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".insv", ".lrv", ".mkv", ".webm"}


def _asset_type(path: Path) -> str:
    return "VIDEO" if path.suffix.lower() in _VIDEO_EXTS else "IMAGE"


def _subfolder(root: Path, path: Path) -> str:
    rel = path.relative_to(root)
    return rel.parts[0] if len(rel.parts) > 1 else "(root)"


def scan_inbound(root: Path, db, *, hash_mode=None) -> list[InboundItem]:
    """Walk `root`, reading each media file's date + GPS and checking it for
    duplication against the snapshot `db`. IO-heavy; the pure logic above
    (`build_existing_trips`, `place`, `build_report`) is what tests target.
    """
    # Imported here so `match`'s pure logic stays importable without exiftool.
    from . import dates as dates_mod
    from . import duplicates as dup_mod
    from . import exif as exif_mod

    if hash_mode is None:
        hash_mode = dup_mod.HashMode.ON_MATCH

    items: list[InboundItem] = []
    for row in exif_mod.read_folder(root):
        path = row.path
        try:
            size = path.stat().st_size
        except OSError:
            continue
        auth = dates_mod.resolve(row)
        when = _naive_utc(auth.dt) if auth is not None else None
        lat = lon = None
        if exif_mod.has_valid_gps(row):
            raw_lat = row.get("Composite:GPSLatitude", "EXIF:GPSLatitude",
                              "XMP:GPSLatitude")
            raw_lon = row.get("Composite:GPSLongitude", "EXIF:GPSLongitude",
                              "XMP:GPSLongitude")
            try:
                lat, lon = float(raw_lat), float(raw_lon)
            except (TypeError, ValueError):
                lat = lon = None
        verdict = dup_mod.classify_one(path, db, hash_mode=hash_mode).verdict
        dup_kind = None if verdict == dup_mod.Verdict.NO_MATCH else verdict.value
        items.append(InboundItem(
            path=path, subfolder=_subfolder(root, path), size=size,
            when=when, lat=lat, lon=lon, asset_type=_asset_type(path),
            dup_kind=dup_kind,
        ))
    return items


__all__ = [
    "ExistingTrip", "InboundItem", "Placement",
    "FolderReport", "EventReport", "MatchReport",
    "build_existing_trips", "place", "build_report", "scan_inbound",
    "DEFAULT_EXTEND_KM",
]
