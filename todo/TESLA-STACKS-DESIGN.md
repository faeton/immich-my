# Tesla dashcam → Immich: stacked, viewable, moderated

Status: **built 2026-10-11** on branch `tesla-dashcam` (`teslacam/` +
`immy stacks apply`), local pilot rendered; **live pilot on n5 pending**.
Implementation notes that differ from the draft: the render folder name is
`<kind>-<car folder stamp>` (not city/reason, so a later metadata fix can't
move the asset's path); `stacks apply` keeps a receipt and reports
`id-changed` when an in-place re-render became a new asset; `scan --geocode`
fills cities from Nominatim (cached) because the Sep15 dump has no
`event.json`; Immich 3.3.1 archives via `visibility: "archive"`.
Supersedes the asset-shape section of [docs/TESLA-PLAN.md](../docs/TESLA-PLAN.md);
the data inventory and retention rules there still hold. Inputs: the plan doc,
the ad-hoc tools in `~/Media/inbox/TeslaDumps/`, measurements on the real
clips, and two outside reviews (Codex, Grok) of the same brief. Where they
disagreed, the choice and the reason are written down.

## Goal

1. Every kept dashcam event is **one tile in the Immich timeline** that plays
   as a multi-camera preview, with the full-resolution angle(s) one flip away
   in the same stack. Date, place and reason are right on web and phone.
2. Before anything reaches Immich, a **moderation pass** on the Mac: drop
   events, pick cameras, and **trim the whole event to one in/out range**
   that applies to every camera.
3. The renderer and the moderation UI live **outside `immy`**; `immy` only
   ships the result and makes the stacks. Raw footage never enters the library.

## Facts the design rests on

| Fact | Source |
|---|---|
| Tesla clips have a keyframe every 0.5 s (2022, 2026) or 0.75 s (2024); every 60 s segment starts on one. Stream-copy cuts at segment boundaries are clean; cuts inside a segment are keyframe-accurate at best and leak leading frames with the concat demuxer. | ffprobe on three generations; ffmpeg concat docs |
| 2024 (TeslaDriveCam) clips are already 2896×1876 at 24 fps but 4 cameras; 2022 clips 1280×960 (4:3); 2025+ 6 cameras at ~36 fps variable. No audio anywhere. | ffprobe |
| Immich 3.3.1 removed `PUT /assets/{id}/original` (replace in place). In an external library the **path is the identity**: a moved file is a new asset, a changed file at the same path is re-read on scan. | Immich v3 migration notes, libraries doc |
| Immich transcodes a video when its codec is not accepted or `min(w,h)` exceeds the target (720 by default). The NAS has no GPU encoder. | Immich transcoding settings |
| The timeline shows only the stack primary; children are flipped through in the viewer. Children are still full assets: thumbnails, CLIP, faces, map pins if they carry GPS. | Immich stacks |
| The video thumbnail is an early keyframe chosen by a scene filter over the start of the file, not a fixed offset. | Immich `ThumbnailConfig` |
| Immich reads `Keys:CreationDate` (with offset), `Keys:GPSCoordinates`, `Make`/`Model` and `Description` from the MP4 container and reverse-geocodes itself. Baked-in values survive a metadata refresh. | existing immy practice (DJI/Insta360) |
| SEI telemetry: H.264 NAL 6, user-data-unregistered, protobuf; field 5 is accelerator %, 14–16 linear accel. Present only in driving clips on recent firmware, never in parked Sentry clips. Reference: `teslamotors/dashcam`. | plan doc + both reviews |

## Options considered

### Asset shape per event

| | Pros | Cons |
|---|---|---|
| **A. Mosaic primary + the cameras chosen per event as children** — *chosen* | 1 tile per event. The viewer's camera checklist decides which full-res angles ride along (all present cameras are checked by default; untick the ones that show nothing). Plates from the front, the incident side from its repeater. | Needs a render. Asset count depends on how much you untick (280–1000). |
| B. Mosaic + all cameras as children | Everything one flip away. | 700–1000 assets, each with thumbs/CLIP/faces; stack flipping through 6 near-identical clips is slow on the phone. |
| C. Front-only primary + other angles | No render, keeps SEI in the primary. | You can't see the side that had the incident without flipping; search/thumbs are road only. |
| D. Mosaic only | Smallest. | No full-res plates. |
| E. PiP / HLS / multi-track | — | Immich plays one stream; no angle switching in web or mobile. Dead end. |

**Mosaic as a playback-ready proxy.** Render the mosaic so Immich never has to
transcode it: H.264 High, yuv420p, **short side ≤ 720**, ~4 Mbps, `+faststart`,
no audio. 6-cam 3×2 → 1440×720 tiles of 480×360 (the 1448×938 sources are
1.54:1, so 480×311 letterboxed in 480×360, or use 1440×622... pick in the
pilot); 4-cam 2×2 → 1280×720 / 960×720 depending on source aspect. A 2172×938
HEVC mosaic would be downscaled to the same 720 rows by the NAS CPU anyway.
Optional **front-emphasis layout** (front 2× tile, others small) is a layout
constant, decided in the pilot by looking at it on the phone.

### Moderation tool

| | Verdict |
|---|---|
| **A. Extend the existing local viewer + its range-request server** — *recommended by both reviews* | Already has the grid, synced scrub, per-event tags/notes/in-out. Add Keep/Drop/Unreviewed, camera checklist, trigger jump, in/out handles, size estimate. |
| B. New web app (FastAPI/HTMX) | Rebuilds what A has. Only if A's code is unmaintainable; it is 34 kB of plain JS and fine. |
| C. Moderate inside Immich with cheap proxies | Puts to-be-deleted clips through scan/ML/transcode on the NAS; no in/out marker exists; description text is not machine-readable and a refresh clobbers it. Rejected. |
| D. TUI | Fine for keep/drop, useless for trimming six cameras. Rejected. |

Phone triage later = serve the same viewer over Tailscale. Not now.

### Where the code lives

| | Verdict |
|---|---|
| **Standalone `teslacam` package in this repo (own `pyproject`, uv), plus one thin `immy` command** — *recommended* | The renderer re-encodes, reads protobuf, runs a web UI: all things `immy`'s rules (immutable originals, XMP, locked columns) are built to prevent. Keep them apart. `immy` keeps what it is good at: rsync, scan, stacks via API, tags via SQL, journal, grouped confirm. |
| `immy tesla` subcommands for everything | Reuses typer/Journal/bloat confirm, but drags ffmpeg filter graphs and a server into a 5,000-line `cli.py`. |
| Separate repo | Loses shared docs/CHANGELOG and the manifest contract would drift. |

The boundary is a **render folder + manifest** that `immy promote` can ship
unchanged and a new **manifest-driven stacker** in `immy` consumes. The stacker
is generic (`immy stacks apply <folder>`), not Tesla-specific, so any future
source that needs "primary + children + tags" per group can use it.

## Design

### Pipeline

```
raw dumps (immutable, stay on the Mac / N5 archive, excluded from the library)
   │  teslacam scan            → events.json (index, SEI, durations, GPS/tz, dupes)
   │  teslacam review          → local viewer; writes decisions.json (per dump)
   │  teslacam render          → render/<YYYY>-tesla-dashcam/<event>/… + stacks.json
   ▼
immy promote render/<YYYY>-tesla-dashcam --into-album "Tesla Dashcam"   (rsync + scan + album)
immy stacks apply <folder> --write                                       (stacks + tags, idempotent)
immy faces purge --tag Source/Tesla                                      (optional, after ML ran)
```

### `teslacam` (new package, `teslacam/` at repo root)

- `scan`: port of `build.py` (segment map, ffprobe cache, SEI parser with the
  corrected field map, thumbs) + the 2024 flat layout (`date__city__reason`),
  RecentClips grouped into **drives** (gap > 120 s; cut chapters at 15 min),
  duplicate segment names across dumps reported and skipped. Output
  `events.json` with per-event: id, kind, cameras, segments, trigger UTC,
  offset + its source, GPS + its source, city, reason, flags (missing
  segments, truncated files, SEI present).
- **Time**: trigger = `creation_time` (UTC) of the trigger segment.
  Offset chain: timezonefinder on the GPS fix → folder-clock minus UTC rounded
  to **15 min**, accepted if |offset| ≤ 14 h → none (asset stays UTC, tag
  `Tesla/TzUnknown`).
- **GPS** chain: median valid SEI fix in the trigger segment → `event.json`
  coordinates (non-zero) → last fix of the previous drive in the same dump if
  under 6 h old → none. Record source + confidence. City from `event.json`
  is a cross-check only; reverse-geocoding is Immich's job.
- `review`: the existing viewer + server moved into the package
  (`teslacam/viewer/`), reading `events.json`, writing **`decisions.json`**
  atomically. Seeded once from the old `tags.json`. Per event: `keep`
  (unreviewed | keep | drop), `cameras` (checklist, all present cameras
  ticked by default), `in_sec`/`out_sec` (default `[trigger−180, trigger+30]`
  clipped to the event; drives default to the whole chapter), `precise`,
  `tags`, `note`, `archive`.
- `render`: pure function of `events.json` + `decisions.json` + renderer
  version. Per kept event:
  - `mosaic.mp4`: exact in/out, xstack, black tile + label for a missing
    camera, `fps=30`, H.264 VideoToolbox proxy as above.
  - `<camera>.mp4` for every camera ticked in the viewer: lossless `-c copy` cut
    of the range at **keyframe granularity** (concat demuxer `inpoint`/`outpoint`;
    Tesla clips carry a keyframe every 0.5 s on the 2022/2026 cars and 0.75 s on
    the 2024 car, so the child starts ≤ 0.75 s before the in point and ends at
    the out point; measured on the Sep15 pilot: start 0.3 s early, end within
    0.1 s, SEI intact). Wall-clock `mov_text` subtitle track with the
    `>>> EVENT <<<` marker, built from the real keyframe start. Opt-in
    `precise: true` per event re-encodes the children to the exact frame.
    (First build copied whole 60 s segments; replaced before the live pilot.)
  - `track.gpx` + `track.json` from SEI, 1 Hz, real UTC timestamps
    (`immy.track` gains a `source=` and proper tz handling, or `teslacam`
    writes its own; decide at implementation).
  - Baked metadata on every mp4: `Keys:CreationDate` = range start, local +
    offset; `QuickTime:CreateDate` UTC; `Keys:GPSCoordinates` on the mosaic
    **only** (one map pin per event); `Keys:Make=Tesla`, `Keys:Model=<car>`;
    `Keys:Description` = `Tesla dashcam · <reason> · <city> · <trigger local time>` +
    tags/note. All members share the same `CreationDate` so the stack never
    splits across days.
  - GPS is **never stripped** (owner's call). No home-radius logic. An event
    can be marked `archive: true` in the viewer, which `immy stacks apply`
    applies after promote.
  - Grouped confirmation per dump (count, GB, minutes); render never deletes.
    Re-render only when the decision hash changes.
- Writes `stacks.json` at the folder root (see manifest).

### Render folder and manifest

```
render/<YYYY>-tesla-dashcam/
  stacks.json
  <YYYY-MM-DD_HH-MM-SS>__<city>__<reason>/
    mosaic.mp4  front.mp4  [back.mp4 …]  track.gpx  track.json  event.json
```

`stacks.json`:

```json
{"schema": 1, "album": "Tesla Dashcam",
 "groups": [{"key": "2026-09-09_19-16-35",
             "primary": "2026-09-09_19-16-35__granada__honk/mosaic.mp4",
             "children": ["2026-09-09_19-16-35__granada__honk/front.mp4"],
             "tags": ["Source/Tesla", "Gear/Camera/Tesla Model 3", "Tesla/Saved",
                      "Tesla/Reason/honk", "Tesla/crash"],
             "archive": false}]}
```

`promote` must not import `track.*`, `event.json`, `stacks.json`: Immich
ignores non-media extensions, but add them to `RSYNC_EXCLUDES`' sibling list
or keep them under a `_meta/` dir if the pilot shows otherwise.

### `immy stacks apply <folder-name> [--write] [--restack]`

Generic. Reads `<originals_root>/<folder>/stacks.json`, resolves each path to
an asset by `originalPath` (one Postgres query, like `stacks.fetch_candidates`),
then per group: if no member is stacked → `create_stack(primary, children)`;
if the stack exists with the same members → skip; if it differs →
only with `--restack`: delete the stack through the API and create it again.
Tags: `upsert_tags` + `trips.link_tags` for every member; `archive: true` →
`PUT /assets` isArchived. Dry-run by default; `--write` applies. Same
report shape as `triage stack-insv`.

### Re-trim flow

Change the range in the viewer → `teslacam render` rewrites the files **at the
same relative paths** → `immy promote` rsyncs (sizes/mtimes differ, so the
second pass picks them up) and scans → Immich re-reads the changed files in
place (same asset id, stack, album, tags kept; this is the single most
important thing the pilot must confirm) → `immy stacks apply` is a no-op
unless a camera was added or removed (then `--restack`).

### What is deliberately not done

- Home-zone privacy handling: none, by decision (GPS stays).
- Faces: Immich runs face detection on everything it scans. After promote,
  `immy faces purge --tag Source/Tesla` deletes the faces on those assets
  (new small command; needed anyway for other sources). Not a per-tag ML
  exclusion, Immich has none.
- CLIP on a mosaic is vague; the front child gives the better embedding. Fine.
- Immich draws no GPX track; the sidecar is for us.
- Raw retention / pruning: unchanged, see TESLA-PLAN.md P5.
- Captions of the trigger frame: later, through the normal immy caption path.

## Pilot (before any bulk render)

One Saved 6-cam event, one 4-cam 2024 event, one Sentry event:
render → promote → stacks apply → check on web **and phone**: one tile, plays
without a transcode job, flip to front, date/offset/place right, map has one
pin; change the in/out and re-run: asset ids unchanged, stack intact. Check
the thumbnail: if it is the start of the clip and not the incident, use the
default window (below) rather than the whole event.

## Decisions taken 2026-10-11 (owner)

1. **Window**: Saved events default to `[trigger−180 s, trigger+30 s]`, widened
   per event in the viewer. Sentry: `[trigger−60 s, trigger+60 s]`. Drives: the
   whole chapter.
2. **Children**: whatever cameras are ticked per event in the viewer; all
   present cameras ticked by default. No fixed front-only rule: "sometimes it
   varies, cannot be front-only."
3. **GPS is never stripped**, no home-radius logic.
4. **RecentClips drives**: scanned and shown as `unreviewed`; rendered only
   when marked Keep.

## Still open (settle in the pilot)

- Car model(s) for `Gear/Camera/Tesla <model>`: is the 4-cam 2022–24 car the
  same car as the 6-cam 2025+ one?
- Mosaic layout: equal tiles vs front-emphasis. Look at both on the phone.
- Whether Immich keeps the asset id when a file changes in place at the same
  path (re-trim flow). If not, re-trims become new assets + `--restack`.
