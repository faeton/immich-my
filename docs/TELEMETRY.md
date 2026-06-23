# Drone telemetry (`immy srt`)

DJI clips record per-frame GPS, altitude and camera settings **only** in the
sibling `.SRT` — the `.MP4`/`.MOV` container carries no GPS. Without harvesting
it, drone footage is location-less in Immich (no map pin, no reverse-geocoded
country/place). `immy srt` reads the `.SRT` and lands that data in Immich
durably.

## Commands

| Command | What it does |
|---|---|
| `immy srt track <trip>` | Emit `<stem>.gpx` (GPX 1.1) + `<stem>.track.json` (full per-frame telemetry + summary) sidecars. |
| `immy srt geotag <trip> [--write]` | Write the first valid fix (takeoff point) to `asset_exif.latitude/longitude` **with a lock**, then reverse-geocode country/state/city. Dry-run by default. |
| `immy srt geocode <trip>|--prefix P [--write]` | Backfill country/state/city for already-located clips, purely from DB coords (no files needed). |
| `immy srt verify-channel <asset>` | One-off probe: prove which DB write survives an Immich metadata refresh for a video. Non-destructive (restores the asset). |

Sidecars are written through `WritablePaths` (see [SIDECAR.md](SIDECAR.md)) — on
the NAS they mirror under `sidecars_root`, never beside the read-only originals.

## The SRT parser (`srt.py`)

`parse_track()` returns one `SrtFrame` per cue: `lat`/`lon`, `rel_alt` (above
takeoff) / `abs_alt` (MSL), and `iso`/`shutter`/`fnum`/`ev`/`focal_len`. It
handles the combined `[rel_alt: .. abs_alt: ..]` bracket, the legacy
`[altitude:]`, and the old `GPS(lat,lon,alt)` form. `first_valid_fix()` skips
the `(0,0)` "null-island" fixes a drone emits before it gets a satellite lock on
takeoff. The legacy first-fix `parse()` API is preserved (used by `dates`,
`backfill_dates`, the `dji-*` audit rules).

## Why GPS for videos needs a locked DB write — not XMP

The audit rule `dji-gps-from-srt` writes GPS to an **XMP sidecar**, which works
for photos but **not videos**: Immich's metadata extraction reads only container
tags for videos (XMP is images-only), and immy's own `asset_exif` insert is
`ON CONFLICT DO NOTHING`, so a drone video already in the library keeps its NULL
coords.

`immy srt verify-channel` proved the durable channel empirically (run live on
n5): for a video, a metadata refresh **clobbers an unlocked
`asset_exif` GPS to NULL**, but an `UPDATE` **+ `lockedProperties` lock**
(tokens `latitude`,`longitude`) **survives**. So `srt geotag` writes coords and
locks them — the only channel that holds.

> The Immich asset-update API (`PUT /api/assets/{id}`) is **not** a safe
> alternative on read-only originals: it queues a `SidecarWrite` that can't land
> and, observed live, wiped a good geotag.

## Why we reverse-geocode ourselves

Immich v2.7.5 only reverse-geocodes coordinates it reads **fresh from a file**
(`metadata.service.ts`: `if (hasGeo(fileExif))`), never the value already in the
DB. Our drone videos have no file GPS and the originals are read-only — so **no
Immich path will ever geocode them** (refresh skips the GPS block for locked
fields; the API route is destructive). We must write `country`/`state`/`city`
ourselves.

`geocode.py` replicates Immich's own `MapRepository.reverseGeocode` against the
**same Postgres** so the place names match the rest of the library exactly:

- nearest `geodata_places` row within `reverseGeocodeMaxDistance` (25 km), via
  the `earthdistance` extension (`ll_to_earth_public` / `earth_box`);
- `naturalearth_countries` polygon containment as a country-only fallback;
- `countryCode`/`admin_a3` → English name via the vendored **i18n-iso-countries
  7.6.0** `en` dataset (`src/immy/data/`), matching Immich's `getName(code, 'en')`.

Validated against 1,500 already-geocoded assets: **country/state/city 100 %
match**. `country`/`state`/`city` are not lockable tokens, but they don't need a
lock — Immich's geo block only fires on file GPS, which these clips never have,
so it never overwrites them.

## Caption context

When captioning a drone clip, `immy process` feeds the altitude + place into the
VLM prompt (`captions.caption(context=…)`): e.g. *"aerial drone shot, ~120 m
above ground, near Cusco."* Place comes from the trip notes `location.name`,
else a cached reverse-geocode. Non-drone media is unaffected (byte-identical
request).
