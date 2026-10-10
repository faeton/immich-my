# Trips

`immy trips` turns a library's day-by-day geography into one Immich album
per trip, across all the years in the library.

```sh
immy trips                         # dry run: the trip table, nothing written
immy trips --csv trips.csv         # …plus a CSV to review
immy trips --since 2024-01-01      # only trips starting in range
immy trips --apply                 # create/update the albums
immy trips --apply --tags          # …and tag assets Trips/<year>/<album>
immy trips --apply --prune         # …and drop assets that moved to another trip
```

## Why not `immy cluster`

`cluster` finds *events*: hours, a few km. A trip is days to weeks and often
several countries, and most of what it holds is media with no GPS at all
(drone, action-cam, mirrorless). Run over a whole library, event clustering
gives thousands of albums; trips give a few hundred.

## How a trip is found

1. **Day track.** Each local calendar day with geotagged assets gets a country
   by majority vote. City and position are then voted only among that day's
   assets in the winning country, so a travel day doesn't get one country and
   another's city.
2. **Bad GPS is dropped first.** Some files carry the sign-flipped coordinate of
   where they were shot, EXIF written without hemisphere refs: `(-lat, -lon)`
   (an Antarctic cruise reads as the Urals) or `(lat, -lon)` (Chicago reads as
   Xinjiang). Two assets on one day at mirror-image positions can't both be
   right, so those days are found positionally. The ghost side is picked by
   majority over ±5 days. Nearby days where every file flipped are caught when
   their mirror lands on real points from that window. Null island `(0, 0)` is
   dropped too.
   **Placeholder dates** go too: an importer that only knew the year (a Takeout
   "Photos from 2019" folder) stamps every such file `2019-01-01 12:00:00`. An
   exact on-the-hour time shared by `placeholder_min` (10) or more assets is
   treated as no date at all. Otherwise it invents a New Year's Day trip and
   pulls those files into any real trip that spans 1 January. The dry run
   prints how many were skipped.
3. **Home.** Days matching a configured home stay are not travel. With no homes
   configured, every day is travel.
4. **Runs.** The remaining days are cut where the **region** changes, a home day
   intervenes, or nothing is geotagged for more than `max_gap_days`.
5. **Stopovers.** A run of at most `transit_days` days that touches another run
   folds into it: the Dubai layover on the way to the Seychelles. Two stints in
   one country split by a one-day hop rejoin.
6. **Membership.** Every live timeline asset whose local date falls inside a
   trip's first..last day joins it, geotagged or not. Trips never overlap.
7. Trips under `min_assets` get no album. With `--tags` they are still
   tagged, so the tag tree lists every trip, small ones included.

### Regions

`immy/src/immy/data/travel_regions.json` groups countries into Oceania,
Southeast Asia, East Asia, South Asia, Central Asia, Caucasus, Middle East,
North Africa, Africa, North America, Central America & Caribbean, and South
America (with Antarctica). A month across five Pacific islands is one trip.

Most of Europe is deliberately **not** grouped. Each country is its own region,
so a weekend in Lisbon between two stints in Spain stays its own trip.
Microstates and territories fold into a neighbour (Andorra and Gibraltar → Spain,
Svalbard → Norway, Monaco → France, …), so stepping over a border doesn't
split a trip.

Override per install:

```yaml
trips:
  regions:
    ES: Iberia      # group Spain and Portugal
    PT: Iberia
    TR: ""          # empty: Türkiye is its own region
```

## Config

All optional; see the `trips:` block in `config.py`'s docstring.

```yaml
trips:
  homes:
    - name: Lisbon          # label only
      until: 2021-06-30     # from:/until: bound the window; unset = open
      lat: 38.72            # centre + radius_km, and/or country:
      lon: -9.14
      radius_km: 50
    - name: Winter base
      from: 2023-11-01
      until: 2024-03-31
      country: ES           # alpha-2 or Immich's English name
  max_gap_days: 3
  transit_days: 1
  min_assets: 20
  placeholder_min: 10
  tag_root: Trips
```

A home with both a country and a centre needs both to match.

## Albums

Names start with the trip's start month so Immich's album list sorts in travel
order:

```
2024-05 Namibia
2026-07 Spain · Valencia                        one city held ≥ half the days
2025-10 Oceania · New Zealand, Australia, Vanuatu +3
2025-02 Southeast Asia · Vietnam, Cambodia, Thailand
```

The city is a city, not a district. Immich names a place after the nearest
GeoNames populated place, which in big cities is often a neighbourhood
(Mokotów, Bang Na, Alvalade). Before the vote, a place rolls up to the city its
own admin area is named after, if that city is within 25 km: Mokotów, Wola and
Ursynów count as Warsaw, Highbury as London, Zhulyany as Kyiv. A town that only
shares a province with its capital stays itself, e.g. Santiago de la Ribera,
40 km from Murcia.

A country that was only a one-day stopover stays out of the name. It is still
in the description, which also carries the itinerary for a multi-country trip,
one **leg** per line:

```
2 Oct – 11 Dec 2025 · 71 days · 6 countries
Tonga · 2–7 Oct
Fiji · 8–9 Oct
Vanuatu · 10–20 Oct
French Polynesia · 21–31 Oct
New Zealand · 1–21 Nov
Australia · 22 Nov – 11 Dec
immy-trip:3f9c1a2b7d40
```

A leg is a stretch in one country. It runs from its first geotagged day to the
day before the next leg starts, so every date in the trip, photo-less ones
included, is in exactly one leg. A one-day hop over a border and back doesn't
split a leg, and territories count as their country.

The marker line is the album's identity. Rename the album or edit the
description freely. While an album keeps the name immy gave it, the name
follows the trip (a new city roll-up, a leg added). Once you rename it, it's
yours. The ledger remembers the description immy last wrote:
while the album still shows exactly that, it follows the trip (a leg's dates
shift, the trip grows). Once you edit it, it's yours and is never overwritten.
`--refresh-descriptions` forces the update anyway, keeping every line that
isn't immy's (header, leg or marker line). When a late
import moves a trip's first day, its key changes. The ledger
(`trips-ledger.json` under `state_root`) records each trip's region and date
range, so the trip is matched back to its album by overlap instead of
duplicated.

## Nesting

Immich albums are flat. Tags nest. `--tags` builds the tree:

```
Trips/
  2025/
    2025-02 Southeast Asia · Vietnam, Cambodia, Thailand/
      Vietnam · 23 Feb – 2 Mar 2025
      Cambodia · 3–5 Mar 2025
      …
    2025-10 Oceania · New Zealand, Australia, Vanuatu +3/
      Tonga · 2–7 Oct 2025
      …
    2025-06 Norway, Svalbard
```

**How tags are written.** Not through Immich's tag-assign API: on
read-only originals, assigning a tag queues a SidecarWrite. That write can't
land, unlocks `asset_exif.tags` anyway, and queues a re-extraction that
replaces the asset's tags with the files' (none of ours). Seen live: thousands
of trip tags gone within minutes. Instead, tags are created through the API
(`PUT /api/tags`, no per-asset jobs), and links go in by SQL together with the
tag list and a `tags` lock. A later extraction keeps the locked list and
rebuilds the links from it. The trade-off: those assets no longer pick up tag
changes from their files (photo `HierarchicalSubject`). And since no API call
assigns them, Immich's tag workflows (3.2+) don't fire for trip tags.

Country names in albums and tags come from the country code (immy's short
names, else GeoNames'), not from Immich's stored string. Immich 3.3 renamed
32 countries in place; trips and tags kept their names.

Each asset gets only the most specific tag: its leg, or the trip for a
one-country trip. Immich resolves a parent tag through its closure table, so
opening a trip or year tag still lists everything below it. Tags are also the
only channel that reaches video assets' metadata (see `TELEMETRY.md`).

## Ownership and `--prune`

The ledger (`trips-ledger.json` under `state_root`) is kept per Immich user,
so a run with `--owner` only ever sees and retires that user's trips. It
records, per trip, exactly what immy put there:
- **Album links:** the album immy created, or adds that returned success. An
  asset that was already in the album (added by hand) is never claimed.
- **Tags:** each trip/leg tag value and the assets immy linked it to. A link
  that already existed (a tag you assigned by hand) is never claimed.

An older ledger (one flat list, no user) is moved over on the next run: each
entry goes to the user who owns its assets. Entries whose tags weren't
tracked yet get them from the database first, with or without `--tags`.

`--prune` removes only those:
- **Moved assets:** an asset that moved to another trip (after a date fix)
  loses its old album link and its old tag. The tag goes from both
  `tag_asset` and the locked `asset_exif.tags` list.
- **Relabelled legs:** a leg whose dates changed gets a new tag. The old one is
  unlinked, and Immich's TagCleanup removes it once empty.
- **Disappeared trips:** a trip that no longer exists (a new home, merged into
  a neighbour) is retired. One that only dropped under `min_assets` isn't: it
  keeps its tags, and immy leaves its album as it is. Its claimed links and tags go;
  the album stays, with anything you added. A dry run lists these first.
  `--since`/`--until` limit this to that range. A trip whose start moved out
  of the range still exists, so it is left alone, not retired.

Tag values can repeat across trips (two Paris trips in one March). A link is
removed only when no surviving trip owns it; one an asset carries into its new
trip moves to that trip, even one outside `--since`/`--until`. A tag that couldn't be created this run (API error)
still counts as wanted, so its links are kept. `--prune` cleans up tags with or
without `--tags`. Without `--prune`, everything is add-only and stale claims
are remembered for a later `--prune`. Assets themselves are never touched.
One `--apply` runs at a time: it locks `trips-ledger.json.lock`.
