# Tesla dashcam ingest — plan (TODO, not built)

Status: **design only**, 2026-09-30. Nothing in `immy` implements this yet.
Source data: `~/Media/inbox/TeslaDumps` (272 GB). Existing ad-hoc tooling
lives in `TeslaDumps/_tools/` and is the starting point.

## What the data is

| Dump | Saved | Sentry | Recent | Total |
|---|---|---|---|---|
| TeslaDump2023 | 13 ev / 9.2 GB | 30 ev / 25 GB | 4 segs / 0.4 GB | 34 GB |
| TeslaDumpAug4 | 55 ev / 113 GB | 3 ev / 4.2 GB | 5 segs / 1.3 GB | 119 GB |
| TeslaDumpSep15 | 18 ev / 43 GB | 6 ev / 3.8 GB | 50 segs / 13 GB | 60 GB |
| TeslaCam/EncryptedClips | — | — | — | 60 GB |

- **125 events** (86 Saved, 39 Sentry), 15.8 h of footage over 50 days, 2022-09 → 2026-09.
  Reasons: honk 44, sentry object detection 33, dashcam icon tapped 23, AEB 1, no metadata 24.
- **Event folder** `<Saved|Sentry>Clips/YYYY-MM-DD_HH-MM-SS/`: ~60 s segments,
  one file per camera `YYYY-MM-DD_HH-MM-SS-<cam>.mp4`, plus `event.json`
  (`timestamp, city, est_lat, est_lon, reason, camera`) and `thumb.png`.
  - 6 cameras (front, back, left/right pillar, left/right repeater) on 82 events;
    4 cameras (no pillars) on the 43 older ones.
- **Codec**: H.264 High, ~36 fps.
  - 2022–23: 1280×960, 3–5 Mbps.
  - 2025–26: front 2896×1876 @ ~10.4 Mbps, others 1448×938 @ ~5.3 Mbps (~280 MB per event-minute).
- **SEI telemetry** (per-frame, inside the H.264 stream): present in driving
  clips from 2026-03 on (~2,150 samples/min of front video): lat/lon, heading,
  speed, gear, steering, brake, blinkers, autopilot state, accel pedal %, linear
  accel x/y/z. **Absent** in 2025 and older clips, and in **all Sentry (parked)
  clips**, even 2026.
- **Time**: mp4 `creation_time` is UTC. Folder names are the car's local clock
  and are **not reliable** (one 2025-07 border event is +3 where the location
  says +2). Rule: UTC `creation_time` + timezone looked up from GPS.
- **EncryptedClips = exact duplicate of TeslaDumpSep15** (the encrypted USB
  originals; Sep15 is the decrypted copy). Same event folders and mp4 names.
  **But** its 24 `event.json` / 25 `thumb.png` are still encrypted and Sep15 has
  none — city/reason for those events exists only there.
- **No other duplicates**: 5,838 mp4 names unique across the three dumps, 0 s
  segment overlap between events, RecentClips shares nothing with Saved/Sentry.

## Existing `_tools/`

- `build.py` — scans Saved + Sentry (skips Recent and TeslaCam). Metadata
  priority `event.json` → SEI GPS → folder name. Reverse-geocodes via Nominatim
  (cached in `geocache.json`), caches ffprobe durations, writes `_thumbs/` and
  `data.js`. Works.
  - Bug: SEI field 5 is accelerator pedal %, not "accel"; fields 14–16 are
    linear acceleration x/y/z.
- `index.html` + `serve.command` — local viewer with range requests (scrubbing
  works); tags / notes / in-out marks persisted to `tags.json` (currently `{}`).
- `immich_export.py` — per tagged event: renders a mosaic (3×2 at 724×469, or
  2×2 for 4-cam; black tiles for missing cams), last 90 s or in/out range,
  VideoToolbox H.264 10 Mbps, exiftool date/GPS/description, uploads, tags
  `tesla/<tag>` + `tesla/reason/<r>`, `PUT /assets` for metadata, "Tesla Dashcam"
  album, progress in `_export/exported.json`. **Never run** (`_export/` empty).
  Known problems:
  - sends `deviceAssetId`/`deviceId` (dropped in Immich 3.x — same break as 047c7fc);
  - timezone derived from folder name − `creation_time` (wrong for the border case);
  - not on immy's tag hierarchy or locked-DB write paths.

## Design

### Asset shape — options

1. **All raw angles into Immich** — ~7.3k one-minute tiles, 213 GB,
   unbrowsable. Rejected.
2. **Front-only stitched video + other angles as a stack** — lossless
   `-c copy` concat keeps SEI and full res, but stacks can't play angles side by
   side; 6×125 assets.
3. **One mosaic per event — recommended.** A derivative we own (originals
   untouched), with GPS, date+offset, `Make=Tesla`, model and description
   baked into the container, so Immich extracts and reverse-geocodes it
   natively — no `lockedProperties` dance like DJI videos need.
   - Optional: stack a lossless full-res **front** concat under it (Saved or
     tagged events only) for reading plates.
   - Encode HEVC VideoToolbox ~6 Mbps; consider a larger front tile.

Budget into Immich: ~43 GB mosaics for every event (~30 GB with Sentry trimmed
to ±60 s around the trigger), plus ~50 GB if front stacks are added for Saved.
Raw 6-angle segments stay on N5 in an archive folder excluded from the library
(e.g. `**/Tesla-raw/**`).

### Metadata

- **Date**: UTC `creation_time` + GPS-derived timezone.
- **GPS**: `event.json` → SEI fix at trigger → last fix of the preceding
  drive (for parked Sentry events with no SEI).
- **Tracks**: `.gpx` + `.track.json` sidecars thinned to 1 Hz, same format
  as `immy srt track` (see [TELEMETRY.md](TELEMETRY.md)).
- **Tags** (immy hierarchy, pushed via `immy tags sync` — the only
  video-safe channel):
  - `Source/Tesla`
  - `Gear/Camera/Tesla <model>`
  - `Tesla/Saved` | `Tesla/Sentry`
  - `Tesla/Reason/{honk,tapped,sentry,aeb}`
  - user tags from `tags.json`
- **Descriptions**: immy's locked-DB path (`AI: ` prefix for generated text).

### Albums

- A "Tesla Dashcam" album.
- Optionally auto-join existing trip albums by time overlap (via `immy cluster`).

### ML

- Caption the **front frame at the trigger moment**, not the mosaic, with
  context (e.g. "dashcam, 57 km/h, near <place>, honk").
- CLIP must be queued explicitly after promote (`--reembed missing`) —
  immy-inserted assets never auto-queue it.
- **Skip face detection** on dashcam assets (strangers would pollute people
  clusters), or delete those faces afterwards.

### Privacy

- **Home zone**: snap or strip GPS for Sentry events inside a configured home
  radius (configured in private config, not in this repo).
- Consider Immich's locked folder or archive for Sentry events.
- Keep Tesla assets out of shared albums by default. Plate/face blur only for
  explicit shared exports.

### Retention (never automatic)

Every re-encode, prune or delete goes through bloat-style **grouped
confirmation**: per dump/trip, GB saved shown, one confirm per group, per-event
opt-out.

- **EncryptedClips (60 GB)**: delete only after the 24 `event.json` + 25
  `thumb.png` are decrypted and every mp4 is verified against Sep15.
- **Sentry**: keep raw, render only the trigger window; flag
  `sentry_no_meta` events as noise candidates.
- **RecentClips (15 GB)**: extract a GPX route (+ optional front timelapse)
  first, then prune with confirmation.

## TODO — phased plan (~5–6 days)

- [ ] **P0 — prerequisites (user, ~0.5 d)**
  - [ ] Decrypt the 24 `event.json` / 25 `thumb.png` left in EncryptedClips (dashcam.tesla.com).
  - [ ] Answer the open questions below.
- [ ] **P1 — `immy tesla scan` (~1.5 d, read-only)**
  - [ ] Port `build.py` into `immy/src/immy/tesla.py`: event index, per-cam segment map.
  - [ ] SEI parser with the corrected field map.
  - [ ] UTC + GPS-timezone resolution; location fallback chain.
  - [ ] Dedup report (incl. EncryptedClips ↔ Sep15 verification) and storage-budget report.
  - [ ] Tests on small trimmed mp4 fixtures (with and without SEI, 4-cam and 6-cam).
  - [ ] Keep the existing viewer + `tags.json` as the curation input.
- [ ] **P2 — `immy tesla render` (~2 d)**
  - [ ] Mosaic render (+ optional lossless front concat), grouped confirmation.
  - [ ] Verify output duration and stream count; resumable via journal.
  - [ ] Bake date/offset, GPS, make/model, description into the container.
  - [ ] Write `.gpx` / `.track.json` sidecars.
- [ ] **P3 — promote (~1 d)**
  - [ ] Into the N5 external library; `tags sync`; front stacks; albums.
  - [ ] Verify on Immich 3.0.2 that GPS + timezone extract from the mosaic and survive a metadata refresh.
- [ ] **P4 — enrichment + privacy (~1 d)**
  - [ ] Trigger-frame captions; explicit CLIP queue.
  - [ ] Home-zone GPS handling; Sentry locked folder or archive; no face detection.
- [ ] **P5 — retention (~0.5 d)**
  - [ ] Grouped-confirm pruning of EncryptedClips, RecentClips, raw Sentry.

## Open questions

1. Mosaic the **whole event** (up to 11 min) or only the **trigger window**
   (90 s / in-out marks)? Different rule for Sentry?
2. Also the full-res front stitch, or is the mosaic enough?
3. Keep raw 6-angle files on N5 forever (213 GB), or delete after render
   (all, or only Sentry + Recent)?
4. Can the 24 encrypted `event.json` be decrypted?
5. Sentry: locked folder, archived, or dropped?
6. RecentClips: keep as route/timelapse, or prune?
7. Car model(s) for `Gear/Camera`? Is the 2022–23 4-cam car a different car
   from the 2025+ 6-cam one?
8. Mosaics into the N5 external library, or direct upload (what
   `immich_export.py` assumed)?
9. Auto-join dashcam events into existing trip albums?
