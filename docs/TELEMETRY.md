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
| `immy srt geotag <trip> [--write] [--relock]` | Write the first valid fix (takeoff point) to `asset_exif.latitude/longitude` **with a lock**, then reverse-geocode country/state/city. Dry-run by default. `--relock` additionally repairs clips that already carry a DB coord but were never locked/geocoded (see below). |
| `immy srt geocode <trip>|--prefix P [--write]` | Backfill country/state/city for already-located clips, purely from DB coords (no files needed). Only touches rows already carrying our lock token. |
| `immy srt verify-channel <asset>` | One-off probe: prove which DB write survives an Immich metadata refresh for a video. Non-destructive (restores the asset). |
| `immy tags sync <trip> [--write]` | Push the trip's notes `tags:` to every asset via Immich's **native Tag API** — the video-safe channel for tags XMP can't reach. See [Tags for videos](#tags-for-videos-immy-tags-sync) below. |
| `immy tags camera <trip> [--write]` | Backfill the Details panel's blank "Camera" row (`asset_exif.make`/`model`) for files whose container carries neither — the DJI-MP4 case — from the trip's notes `Gear/Camera/<make> <model>` tag. See [Camera model for videos](#camera-model-for-videos-immy-tags-camera) below. |

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

## Clips with a map pin but no place name (`--relock`)

Found live on n5 (2026-07): a batch of drone clips across ~30 trips had
`asset_exif.latitude/longitude` populated — sometimes exactly matching the
`.SRT` first fix — but `lockedProperties` **empty** and `country` **NULL**.
The Info panel drops a correct pin but shows "Add a location", because the
place name was never written.

Both existing commands silently skip these rows: `srt geotag`'s only
idempotency check is "does the DB already have *a* coord" (it does, so it
skips), and `srt geocode` requires the lock token as proof-of-ownership
before touching a row (a deliberate guard against overwriting a location you
pinned by hand in the app) — these rows aren't locked, so it skips them too.
They fall through the gap between the two safety nets.

`srt geotag --relock` closes it: for a row with an existing DB coord that is
**not** locked, it computes the SRT's first-fix independently and only acts
if the DB coord is within 2 km of that fix (`_RELOCK_TOLERANCE_M` in
`srtgeo.py`) — close enough to be confidently immy/DJI-derived, not a manual
pin. On a match it locks the existing coord and reverse-geocodes it in the
same pass; anything farther than that is left untouched (`skip-mismatch`) as
presumptively user-set.

How these rows got unlocked coords in the first place wasn't fully
reconstructed — the working theory is a partial/manual run predating the
lock-on-write guarantee, or Immich's own scanner reading a `dji-gps-from-srt`
`.xmp` sidecar for the small number of assets where its exiftool pass
happens to touch XMP. Either way, `--relock` is the durable fix going
forward: any row it can't confidently repair it leaves alone.

## Tags for videos (`immy tags sync`)

The same video/XMP blind spot documented above for GPS applies to tags. The
`trip-tags-from-notes` audit rule writes the trip's notes `tags:`
(`Gear/Camera/*`, `Events/*`, `Source/*`, …) to each file's `.xmp` sidecar —
Immich reads that back for photos on its own library scan, never for videos.
So a DJI/Insta360/GoPro clip's device tag never reaches Immich's UI or search
unless pushed through the native Tag API directly (`PUT /api/tags` +
`PUT /api/tags/{id}/assets` — the same API `promote --tag` uses for one-off
merge markers like `post-edited`).

`immy tags sync <trip> [--write]` is that push, applied to the *whole* trip's
notes tag set (not just whatever a manual `--tag` invocation happened to
pass): `tagsync.py` recomputes each file's tag set with the exact same
per-camera matching logic the XMP rule uses (`rules/trip_tags.tags_for_file`,
extracted so the two channels can't disagree), resolves each file to its
Immich asset id the same way `srt geotag` does, and upserts + attaches every
tag. Idempotent — safe to re-run after adding new footage to a trip.

## Camera model for videos (`immy tags camera`)

DJI's MP4 container carries no `Make`/`Model` tags at all (confirmed empty
live, not assumed — `EXIF:Make`/`Model` and `ItemList:Encoder`/`QuickTime:
Encoder` are simply absent from real DJI video files), so Immich's Details
panel "Camera" row sits blank for every DJI clip even after GPS and tags
are fixed. DJI *stills* do carry a bare module code as Model (`FC8282`).

Verified live 2026-07-12 with a `srt verify-channel`-style probe (write a
sentinel `make`/`model`, trigger a real metadata refresh, observe): unlike
GPS, an **unlocked** make/model write survives a refresh too — Immich only
ever *sets* these fields from a fresh file read, it never nulls them out
when the file has none. So strictly a lock isn't required for durability
here, but `tags camera` locks anyway (`make`, `model` tokens), matching the
GPS precedent as a safety net against a future Immich version behaving
differently.

`immy tags camera <trip> [--write]` resolves every file through
`devices.resolve()` — the SAME owner-confirmed friendly-name table `immy
process` uses at ingest time (`devices.py`; module codes like `FC8282` →
`"DJI Air 3"`) — never a raw code. Primary signal is the file's own raw
EXIF/Encoder; for DJI video (which has none), it falls back to the trip's
`Gear/Camera/<code>` notes tag, itself run through the same table rather
than used raw. `make`/model` values in the table deliberately don't repeat
"DJI" (Immich concatenates make+model for display; a `"DJI"` + `"DJI Air 3"`
model would double up).

Idempotent, in two directions: an asset we already locked gets silently
re-corrected if the resolved value has since changed (the friendly-name
table gaining an entry, or a stale earlier write of this same command). An
asset with an *unlocked* existing value (Immich's own extraction) is left
alone — unless that existing value is itself a known-raw code the table
maps to something different, in which case it's upgraded: a confident
lookup against data we already recognize, never a guess at data we don't.

Found and fixed live 2026-07-12: 632 pre-existing assets across the library
(predating `devices.py`'s existence) carried a raw module code this way,
plus a still-undiscovered DJI video Encoder string (`"DJI Mini5Pro"`, no
space — 518 assets) not yet in the table. ~130 assets remain stuck on a raw
code because of a pre-existing, unrelated data-integrity issue: duplicate
`asset` rows sharing the same `originalPath` (624 across the library) make
`resolve_asset_id`'s un-ordered `SELECT` + `fetchone()` nondeterministic for
those specific files — it may resolve to either duplicate on a given run.
Out of scope here — that's what the separate dedup pipeline (`immy dedup`)
exists to resolve.

## Caption context

When captioning a drone clip, `immy process` feeds the altitude + place into the
VLM prompt (`captions.caption(context=…)`): e.g. *"aerial drone shot, ~120 m
above ground, near Cusco."* Place comes from the trip notes `location.name`,
else a cached reverse-geocode. Non-drone media is unaffected (byte-identical
request).
