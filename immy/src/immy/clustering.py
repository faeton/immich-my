"""Phase 4 — event clustering + auto-album naming.

Take every asset with `(dateTimeOriginal, latitude, longitude)` from
`asset_exif`, group it into events by time+space proximity, and name
each group from the city/country values Immich's own reverse-geocode
worker already wrote. The CLI layer (`immy cluster`) creates or
updates an album per event in Immich via the existing `ImmichClient`.

Why this lives in immy and not Immich:
- Immich has per-trip / per-day smart-albums via SQL queries the user
  writes; there's no built-in `(time, space)` event clusterer.
- Our trip folders are *not* events — a two-week Bolivia folder holds
  a dozen sub-events (Coroico, Salar de Uyuni, Potosí…). Clustering is
  what turns that bulk ingest into something the viewer actually wants
  to click through.

Algorithm is deliberately simple: sweep sorted-by-time and start a new
event when gap > `max_gap_hours` **or** distance-from-centroid
> `max_km`. DBSCAN-style density clustering is overkill — the
time+space signal is already bimodal (you're either still at the same
place or you drove two hours to the next one).

Idempotency: every auto-generated album carries a
`IMMY_CLUSTER_MARKER` line in its description with a stable hash. On
re-run the CLI finds that marker, updates the asset list, and leaves
user-edited names/descriptions alone.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta


IMMY_CLUSTER_MARKER = "immy-cluster:"


# Defaults tuned against real trip data in the target DB: people move
# through cities in ~hours (not minutes, not a full day), and "same
# place" at GPS accuracy is typically within a couple km (hotel → tour
# location → hotel, all within a small town). Override per-run via CLI
# flags when a trip has a different cadence (e.g. a driving road trip
# might want 1 h / 20 km to split more aggressively).
DEFAULT_MAX_GAP_HOURS = 4.0
DEFAULT_MAX_KM = 5.0

# Drop tiny clusters so singletons (one screenshot in Novi Beograd)
# don't pollute the album list. 3 is the floor at which "event" feels
# meaningful; users can lower via CLI for debugging.
DEFAULT_MIN_ASSETS = 3


@dataclass(frozen=True)
class AssetPoint:
    """One asset's cluster-relevant attributes, as pulled from the DB.

    Kept minimal and hashable so clustering is a pure transform over
    Python values — no DB access inside the algorithm. The CLI layer
    is responsible for fetching rows and filtering out soft-deleted
    assets.
    """

    asset_id: str
    when: datetime
    lat: float
    lon: float
    city: str | None
    country: str | None


@dataclass
class Cluster:
    """A time-contiguous, space-contiguous group of `AssetPoint`s.

    Mutable during construction (the sweep appends) and treated as
    immutable after clustering returns. Derived properties are
    computed on demand; there's no cached invariant that would break
    if callers mutate `assets` (don't).
    """

    assets: list[AssetPoint] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.assets[0].when

    @property
    def end(self) -> datetime:
        return self.assets[-1].when

    @property
    def centroid(self) -> tuple[float, float]:
        lats = [p.lat for p in self.assets]
        lons = [p.lon for p in self.assets]
        return sum(lats) / len(lats), sum(lons) / len(lons)

    @property
    def dominant_city(self) -> str | None:
        return _most_common([p.city for p in self.assets if p.city])

    @property
    def dominant_country(self) -> str | None:
        return _most_common([p.country for p in self.assets if p.country])

    def name(self) -> str:
        return name_for_cluster(self)

    def stable_key(self) -> str:
        return stable_key_for_cluster(self)


def _most_common(values: list[str]) -> str | None:
    """Mode over a list, or None when empty. Ties break arbitrarily —
    Counter.most_common preserves insertion order, which is sorted-by-
    time from the caller, so in practice the earliest city wins a tie.
    """
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance between two (lat, lon) pairs in km.

    Plain haversine — no ellipsoid correction. At 5 km scale the
    earth-is-a-sphere error is sub-metre, well below GPS noise.
    """
    r = 6371.0  # mean earth radius, km
    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlambda = math.radians(b_lon - a_lon)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cluster_assets(
    points: list[AssetPoint],
    *,
    max_gap_hours: float = DEFAULT_MAX_GAP_HOURS,
    max_km: float = DEFAULT_MAX_KM,
    min_assets: int = DEFAULT_MIN_ASSETS,
) -> list[Cluster]:
    """Group sorted-by-time points into time+space-contiguous clusters.

    Sweep algorithm:
    1. Sort input by `when`.
    2. For each point, check against the *running* cluster:
       - If gap-to-previous > max_gap_hours → start new cluster.
       - Else if distance-to-cluster-centroid > max_km → start new cluster.
       - Else → append to current cluster (centroid shifts on append).
    3. Drop clusters with < `min_assets` points.

    The centroid is cheap to recompute (mean of lat, lon) and good
    enough at km scale. We deliberately don't use a rolling window —
    someone at the same hotel for 3 days should land in one cluster,
    not three, as long as photos are taken within `max_gap_hours` of
    each other.
    """
    if not points:
        return []
    ordered = sorted(points, key=lambda p: p.when)
    gap = timedelta(hours=max_gap_hours)
    clusters: list[Cluster] = []
    current = Cluster(assets=[ordered[0]])
    for p in ordered[1:]:
        last = current.assets[-1]
        time_gap = p.when - last.when
        cen_lat, cen_lon = current.centroid
        dist = haversine_km(cen_lat, cen_lon, p.lat, p.lon)
        if time_gap > gap or dist > max_km:
            clusters.append(current)
            current = Cluster(assets=[p])
        else:
            current.assets.append(p)
    clusters.append(current)
    return [c for c in clusters if len(c.assets) >= min_assets]


def _format_date_range(start: datetime, end: datetime) -> str:
    """`15 Apr 2024` / `15–17 Apr 2024` / `29 Apr – 3 May 2024`.

    Always use the same locale-independent format so album names are
    sortable and greppable. Short month name avoids ambiguity between
    US (04/15) and everywhere-else (15/04).
    """
    if start.date() == end.date():
        return start.strftime("%-d %b %Y")
    if start.month == end.month and start.year == end.year:
        return f"{start.strftime('%-d')}–{end.strftime('%-d %b %Y')}"
    if start.year == end.year:
        return f"{start.strftime('%-d %b')} – {end.strftime('%-d %b %Y')}"
    return f"{start.strftime('%-d %b %Y')} – {end.strftime('%-d %b %Y')}"


def name_for_cluster(cluster: Cluster) -> str:
    """Human-readable album name: `<city>, <country> — <date>`.

    Drops parts that aren't available: a GPS point in the ocean has
    no city, a city without a country shouldn't happen in Immich's
    geocoder, but handle both gracefully. Last resort: just a date,
    which is better than an empty title.
    """
    city = cluster.dominant_city
    country = cluster.dominant_country
    date = _format_date_range(cluster.start, cluster.end)
    parts: list[str] = []
    if city and country:
        parts.append(f"{city}, {country}")
    elif city:
        parts.append(city)
    elif country:
        parts.append(country)
    parts.append(date)
    return " — ".join(parts)  # em-dash


def stable_key_for_cluster(cluster: Cluster) -> str:
    """12-char hex key that survives asset-set churn at the edges.

    Computed from rounded centroid + start date (day precision) so
    adding a late-arriving photo to a cluster doesn't invalidate the
    key and spawn a duplicate album. The key ends up in the album
    description as `immy-cluster:<key>`; on re-run we match the album
    by that line and update its asset list in place.
    """
    cen_lat, cen_lon = cluster.centroid
    seed = f"{cen_lat:.3f},{cen_lon:.3f}|{cluster.start.strftime('%Y-%m-%d')}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def cluster_marker_line(key: str) -> str:
    """The identifier line we embed in the album description.

    Kept simple — one line, stable prefix, so `grep` or a startswith
    check locates it without regex. Appended to (not replacing) the
    existing description so user-typed text above the marker is
    preserved across re-runs.
    """
    return f"{IMMY_CLUSTER_MARKER}{key}"


def extract_cluster_key(description: str | None) -> str | None:
    """Pull `<key>` out of an album description if an immy marker exists.

    Scans line by line so user text that happens to contain the
    marker prefix elsewhere (quoted, inside a sentence) isn't
    mis-parsed. Returns None if no matching line is found.
    """
    if not description:
        return None
    for line in description.splitlines():
        s = line.strip()
        if s.startswith(IMMY_CLUSTER_MARKER):
            return s[len(IMMY_CLUSTER_MARKER):].strip() or None
    return None


__all__ = [
    "IMMY_CLUSTER_MARKER",
    "DEFAULT_MAX_GAP_HOURS", "DEFAULT_MAX_KM", "DEFAULT_MIN_ASSETS",
    "AssetPoint", "Cluster",
    "haversine_km", "cluster_assets",
    "name_for_cluster", "stable_key_for_cluster",
    "cluster_marker_line", "extract_cluster_key",
]
