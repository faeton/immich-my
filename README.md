# immich-my

A personal media catalog and curator built on top of [Immich](https://immich.app),
tuned for a workflow with cameras, drones, 360 footage, iPhone and Apple Photos,
and remote/offline storage. The Synology DS923+ is the always-on core; a MacBook
(and optionally a future Mac mini / N100) does the heavy neural-engine work.

This repo is the **plan and operating manual**. Phase 0 (stock Immich on the
Syno) is live; the sidecar layers described below are still ahead of us.

## Why this exists

Immich is the strongest open-source base available today — fast UI, mobile
backup, CLIP search, face recognition, pHash duplicate detection, active weekly
releases. But it is missing pieces for this workflow:

- No Whisper transcripts, no BLIP/LLaVA captions feeding search.
- No automatic event/trip grouping by time + location.
- No 360 (`.insv/.insp/.lrv`) or DJI telemetry handling.
- No Apple Photos / Photos.app library puller.
- No metadata-gap bulk-fill UI ("assign this location to all 80 files from this day").
- No "ghost asset" support for originals that live on drives you unplug.

Everything above is built as a **sidecar** that speaks to Immich over its public
REST API. No forking. Upgrades stay clean.

## The two-box architecture in one picture

```
DS923+  (always on, 20 GB RAM, R1600, no iGPU)      MacBook Apple Silicon
├── Immich server + web UI                          ├── immich-ml-metal
├── Postgres (catalog, faces, embeddings)   ◀──API──┤   (ANE face detect,
├── Redis                                           │    CoreML ArcFace,
├── originals (HDD pool, read-only mount)           │    MLX CLIP)
├── derivatives (NVMe pool: thumbs,                 ├── curator sidecar
│   proxies, pgvector, captions, transcripts)       │   (osxphotos pull, 360/DJI
├── nominatim (reverse geocoding)                   │    preprocess, whisper,
└── SMB/NFS shares so the Mac can edit              │    captioner, events, gap-fill)
    originals directly                              └── VideoToolbox proxy gen
```

State lives on the NAS. Compute is replaceable. The Mac can sleep; jobs queue
up; draining is idempotent.

## How it hypothetically works, day by day

**Importing from a camera (SD card)**
1. Plug SD card into the Mac or Syno. Rsync to `/library/inbox/<camera>/YYYY-MM-DD/`.
2. Sidecar watches the inbox, normalises sidecar files (`.LRV`, `.SRT`, `.XMP`),
   runs exiftool on the header only, extracts embedded JPEG previews for RAW.
3. Moves the originals into `/library/originals/` and triggers Immich external-library scan.
4. Thumbnail/proxy/embedding jobs queue up. Browsing works within seconds;
   enrichment finishes in the background.

**iPhone / Apple Photos**
- Immich's iOS app auto-backs up the camera roll to the Syno on Wi-Fi.
- Nightly `osxphotos export --update` pulls anything that lives in the macOS
  Photos library (imports, edits, people names) into the same inbox pipeline.
- iCloud-only items pulled by `icloudpd` on the Mac.

**Drones and 360**
- `.insv/.insp` pairs detected; LRV low-res proxies harvested so browsing never
  touches the 20 GB original stitched file.
- DJI `.SRT`/`.LRF` sidecars parsed for GPS and altitude; written as XMP so
  Immich places the shot on its map.

**Remote and offline drives**
- Archive drives (2024 trips, etc.) mounted via SMB/NFS or `rclone mount`.
- Catalog-only mode: ingest header + embedded preview + AI embeddings, then
  unplug the drive. Search, browse, thumbnail view still work. Full-resolution
  open shows "volume 'archive-2024' is offline; plug in or mount to retrieve."

**Faces**
- InsightFace `buffalo_l` (accuracy-first). First backfill runs on the MacBook
  via `immich-ml-metal` (Neural Engine) — roughly an hour for 50k photos.
  Steady state: as new photos land, embeddings compute in seconds.
- Label the top ~20 clusters once; the rest auto-attach.

**Search**
- CLIP free-text: "sunset over water", "dog on a beach", "red tram".
- By person: "Anna AND Lisbon AND 2024".
- By transcript: video talk content indexed via Whisper, searchable like text.
- By AI caption: descriptions from moondream/BLIP supplement CLIP for long-tail.

**Metadata gap-fill**
- Sidecar web app shows assets missing GPS or timestamp, grouped by nearest
  date/location cluster. One click applies to the whole group.

**Events**
- Nightly DBSCAN on `(time, lat, lon)` creates albums like `2026-04 Lisbon`
  with human-readable names from the reverse-geocoder.

**Editing originals**
- The Mac mounts the Syno's `originals` share via SMB. Editing apps open files
  directly — no re-download. Proxies never leave the Syno's NVMe pool.

## Hardware snapshot

- **Storage + web + DB** → Synology DS923+, Ryzen R1600 (2c/4t), 20 GB ECC RAM,
  HDD pool for originals, NVMe storage volume for derivatives, 1 GbE×2 (LAG or
  future 10 GbE via E10G22-T1-Mini).
- **Compute** → MacBook Apple Silicon with OrbStack or Docker Desktop running
  `immich-ml-metal` + the curator sidecar. Replaceable with a Mac mini / N100
  later without touching the design.

Full verified specs and sources in [docs/HARDWARE.md](docs/HARDWARE.md).

## What's in this folder

| File | What it covers |
|---|---|
| [docs/LANDSCAPE.md](docs/LANDSCAPE.md) | Survey of open-source alternatives and why Immich won |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Technical design: tiered storage, mount adapters, pipelines, queues |
| [docs/SIDECAR.md](docs/SIDECAR.md) | Sidecar internals: DB choice, queue schema, worker-harness contract, process layout |
| [docs/HARDWARE.md](docs/HARDWARE.md) | DS923+ + MacBook specifics, verified specs, performance expectations |
| [docs/PLAN.md](docs/PLAN.md) | Phased build plan, milestones, what's custom vs stock |
| [docs/DEPLOY.md](docs/DEPLOY.md) | As-deployed operating manual: paths, compose, onboarding choices |
| [docs/TESTING.md](docs/TESTING.md) | Acceptance tests per phase + ad-hoc smoke checks |

## Status

- **Phase 0 — Base stack**: done. Stock Immich running on the DS923+ under
  Container Manager, docker project `fnim`, data under `/volume1/faeton-immi/`,
  reached over Tailscale. Details in [docs/DEPLOY.md](docs/DEPLOY.md).
- **Phase Y — direct-to-Immich-DB pre-processing**: done. `immy process →
  promote` lands asset + EXIF + derivatives (thumbnail/preview/encoded_video)
  + CLIP + faces straight into Postgres without touching Immich's scan
  pipeline. InsightFace `buffalo_l` runs on the ANE via onnxruntime-CoreML;
  MLX-CLIP and Apple Vision cover the rest. `immich-accelerator` removed
  2026-04-20 — `immy` is the sole ingestion path. See
  [docs/PLAN.md](docs/PLAN.md) for the Y.1–Y.6 ladder.
- **Next up**: Phase 2c residuals (bloat re-encode QoL), Phase 3 enrichment
  (Whisper transcripts, BLIP/moondream captions, pHash duplicates), Phase 4
  DBSCAN event clustering.
