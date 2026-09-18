# Placing an inbound dump (`immy match`)

Before importing a folder of media you usually want three questions answered:
**what's already in Immich** (don't re-import duplicates), **which existing
trip does the rest belong to** (so it lands in the right album), and **what's
genuinely new** (a trip that doesn't exist yet). `immy match` answers all three
**read-only and fully offline** — it proposes, it never writes.

It runs against a portable **v2 snapshot** of the library (see
[the snapshot section](#the-v2-snapshot)), so it needs no live Immich/Postgres
connection at match time — only when the snapshot was last refreshed. That fits
the mobile-Mac / bad-uplink reality: snapshot once on the tailnet, then triage
inbound dumps anywhere.

## Usage

```sh
immy snapshot                          # refresh ~/.immy/library-snapshot.sqlite (needs DB)
immy match ~/Incoming/2026-05-some-trip   # place it against the library (offline)
```

| Option | Default | Meaning |
|---|---|---|
| `PATH` (arg) | — | Inbound media root to place. Walked recursively. |
| `--snapshot` | `~/.immy/library-snapshot.sqlite` | v2 SQLite snapshot from `immy snapshot`. |
| `--thorough` | off | Hash **every** file for dedup (catches renames). Slow — reads the whole tree. Default hashes only on a name+size hit. |
| `--fast` / `--no-verify` | off | Trust a name+size match as a duplicate — skip SHA1 entirely. On a ~2 TB already-promoted tree this is ~2 min vs ~50 min, at the cost of missing a same-name/same-size/different-bytes file. Mutually exclusive with `--thorough`. |
| `--max-km` | `clustering.DEFAULT_MAX_KM` | How far a clip may sit from a trip and still count as part of it. |
| `--max-gap-hours` | `clustering.DEFAULT_MAX_GAP_HOURS` | Date slack (hours) around a trip's range for placement. |

A v1 snapshot is rejected (`require_schema`) — re-run `immy snapshot` to get v2.

## What it reports

Output has three sections (`cli.py::match`):

1. **By subfolder** — each top-level dir under `PATH`, with a tally of
   `matched` / `extends` / `new` / `dup`, the existing trip(s) it maps to, and
   a `⚠ spans multiple trips` flag when one folder straddles more than one trip
   (a sign the folder should be split before import).
2. **By event** — the inbound media is *self-clustered* (same time+space sweep
   as `immy cluster`) into events, each placed independently. This catches the
   opposite case: several days/locations dumped into one flat folder.
3. **Dedup tally + caveat** — `N/total already in Immich · M to place`, plus a
   count of **GPS-less** clips placed by date alone (lower confidence; see
   [the GPS-less fallback](#gps-less-drone--video)).

The two groupings are deliberately shown side by side: folder structure and
actual event structure often disagree, and you want to see both before
committing to album assignments.

## How it works

Three signals are folded into one report (`match.py`).

### 1. Dedup

Every inbound file is classified against the snapshot by `(filename, size)` and
— on a hit — SHA1, reusing the `find-duplicates` classifier
(`duplicates.classify_one`). Verdicts map to `dup_kind`:

- `exact` — SHA1 match (definitely already imported)
- `likely` — name+size match (`HashMode.ON_MATCH` didn't need to hash, or did and agreed)
- `name-only` — name matches but size differs (**not** treated as a dup)

`--thorough` switches to `HashMode.THOROUGH`, hashing every file so a renamed
copy is still caught. Duplicates are excluded from trip placement.

**Cost note.** `ON_MATCH` still reads the *full bytes* of every file whose
name+size matches the snapshot (to confirm byte-identity). When the inbound
tree is already ~100 % promoted, almost everything matches, so dedup reads
nearly the whole tree off disk — e.g. `~/Media/Trips` (≈2.1 TB) takes ~50 min
at SSD speed, dominated entirely by SHA1, not exiftool. `--fast` /
`--no-verify` (`HashMode.FAST`) trusts the name+size hit and skips hashing,
landing those as `likely` dups — the same run drops to ~2 min (exiftool-bound).
Use it when you trust the tree isn't hiding a same-name/same-size/different-bytes
file; fall back to the default when you need byte-level certainty.

### 2. Reconstructing existing trips

`build_existing_trips()` rebuilds the library's trips two ways and uses them
together:

- **Albums** — every album in the snapshot becomes a candidate trip, keyed by
  its **name** (the curated trip name). Bounds are recomputed from member
  assets, not trusted from metadata:
  - **dates** are IQR-fenced (`_robust_date_bounds`) — one misdated frame (a
    2016 photo in a 2025 trip) can't blow the window to a 9-year span that then
    date-matches everything;
  - **centroid** is the **median** lat/lon and **radius** is the **90th
    percentile** member distance (`_trip_from_members`) — both robust to a
    single mislocated asset that would otherwise drag a mean centroid or
    inflate a max-distance radius across the planet.
  - An album whose members carry no usable capture date yields no bounds; its
    members are **not** claimed, so they fall through to clustering below.
- **Raw points** — every asset *not* claimed by an album, that has both a date
  and coords, is re-clustered with `clustering.cluster_assets` (the same sweep
  `immy cluster` uses) to synthesise implicit trips for un-albumed media.

> **Why keep every album, not just immy-cluster-marked ones?** Real albums are
> overwhelmingly trip-named but created by `promote`, which carries **no**
> `immy-cluster:` marker. Filtering to marked-only would drop them all, and
> `match` would re-synthesise trips from raw points — losing the curated names.
> So `fetch_albums` keeps all of them; `marker_key` is parsed but optional.

### 3. Placement

`place(when, lat, lon, trips)` scores one item against every trip and returns
the best by this precedence:

```
geo-matched  >  date-only-matched  >  geo-extends  >  date-only-extends  >  new
```

- **matched** — within the trip's date range **and** (if both have coords)
  within `radius_km + max_km`.
- **extends** — date-adjacent (inside the `max_gap_hours` slack around the
  range) or just outside the radius but within `DEFAULT_EXTEND_KM` (50 km).
- **new** — no trip inside the date+distance window (or the item has no date).

Datetimes are normalised to naive-UTC (`_naive_utc`) so DB-aware and EXIF-naive
times compare; placement only needs day-scale accuracy. A multi-day **event**
is placed at its **date midpoint**, not its start, so a dump straddling the
edge of a trip's range still matches.

### GPS-less (drone & video)

Drone and video clips routinely carry **no EXIF GPS** — for DJI it lives in the
sibling `.SRT` (see [TELEMETRY.md](TELEMETRY.md)). Those items fall back to
**date-only** placement, labelled lower-confidence in the report. The tally
suggests `immy srt geotag` to lift them to geo confidence before import.

## The v2 snapshot

`immy match` is only as good as the snapshot. Schema **v2** (`snapshot.py`,
`SCHEMA_VERSION = 2`) added what placement needs over v1:

- `assets` gained `lat` / `lon` / `city` / `country` (+ in the SELECT).
- New `albums` (`album_id`, `name`, `marker_key`) and `album_assets`
  (membership) tables, so trips reconstruct offline.

`require_schema` rejects a v1 file; `find-duplicates` stays v1-compatible (it
only needs filename/size/checksum). Refreshing the snapshot needs Immich
Postgres access — run it while on the tailnet, then match offline.

## Scope & caveats

- **Read-only.** `match` never writes Immich, sidecars, or originals. It's a
  triage report; you still run `immy promote` / import to act on it.
- **Snapshot freshness.** Trips and dedup reflect the library *as of the last
  `immy snapshot`*. A trip imported after the snapshot reads as `new`.
- **Names are identity.** Album name = trip name. Two albums with the same name
  collapse; a renamed album reads as a different trip until re-snapshotted.
- **Pure core, IO at the edge.** `build_existing_trips` / `place` /
  `build_report` are pure and unit-tested (`tests/test_match.py`); only
  `scan_inbound` touches exiftool + the filesystem.

## Related

- [`immy cluster`](../README.md) — the geo-date clustering `match` reuses.
- [`immy find-duplicates`](IMMICH-INGEST.md) — the dedup classifier `match` reuses.
- [TELEMETRY.md](TELEMETRY.md) — `immy srt geotag` for GPS-less drone clips.
- [OFFLINE-RUNBOOK.md](OFFLINE-RUNBOOK.md) — working offline against a snapshot.
