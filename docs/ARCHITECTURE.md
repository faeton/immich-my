# Architecture

State lives on the Synology. Compute is replaceable. The browse path never
touches remote originals. Enrichment is idempotent and queue-based.

## Components

```
                       ┌─────────── MacBook (bursty) ───────────┐
                       │                                        │
                       │  immich-ml-metal                        │
                       │    • Apple Vision face detect (ANE)     │
                       │    • InsightFace ArcFace (CoreML)       │
                       │    • MLX CLIP                           │
                       │                                        │
                       │  curator sidecar                        │
                       │    • ingest watcher                     │
                       │    • preview extractor                  │
                       │    • 360 / DJI preprocess               │
                       │    • whisper.cpp (Metal)                │
                       │    • captioner (moondream / BLIP)       │
                       │    • event clustering                   │
                       │    • gap-fill web UI                    │
                       │    • osxphotos / icloudpd pullers       │
                       │    • VideoToolbox proxy gen             │
                       │                                        │
                       └──────────┬─────────────────────────────┘
                                  │ HTTP (Immich REST) + SMB (library)
                                  ▼
┌───────────────────── DS923+ (always on) ──────────────────────┐
│                                                              │
│  Immich server (web UI + API + jobs)                         │
│  Postgres (catalog, pgvector, faces, events)                 │
│  Redis                                                        │
│  Nominatim (reverse geocoding)                               │
│                                                              │
│  Storage layout:                                             │
│    /volume1/library/inbox/        (writeable, polled)        │
│    /volume1/library/originals/    (read-only external lib)   │
│    /volumeNVMe/immich/thumbs/     (derivatives)              │
│    /volumeNVMe/immich/proxies/    (H.264 proxies)            │
│    /volumeNVMe/immich/transcripts/                           │
│    /volumeNVMe/postgres/          (chattr +C, BTRFS no-CoW)  │
│                                                              │
│  Fallback ML: Immich's stock CPU ML container (always on)    │
│  Immich ML URL → balanced: Mac primary, Syno fallback        │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Storage tiers

| Tier | Where | What lives there | Readable when Mac asleep? |
|---|---|---|---|
| 0 — hot | Syno NVMe pool | thumbs, proxies, transcripts, captions, embeddings, Postgres | ✅ |
| 1 — originals | Syno HDD pool, NAS over SMB, external drives, rclone | untouched originals | ✅ if mounted |
| 2 — cold / offline | Unplugged drives, S3 archive | catalog stubs only (ghost assets) | ✅ browse only |

Rule: after first ingest, **originals are never re-read for browsing**. Only
explicit "open original" reads tier 1.

## Mount adapters

One container module per source. Each owns its mount lifecycle.

| Source | Tool | Notes |
|---|---|---|
| NAS / remote Mac | `mount_smbfs`, NFS, `sshfs` | `autofs` or systemd `.automount` so scanner doesn't hang offline |
| Cloud (Drive, S3, Dropbox) | `rclone mount --vfs-cache-mode full` | Cache capped on tier-0 |
| iPhone / iCloud | `icloudpd` | Scheduled sync, not a live mount |
| Apple Photos | `osxphotos export --update` | Preserves people, keywords, edits |
| Offline drives | catalog-only | Ghost assets |

Adapter responsibilities:
- Health-check: "is this mount up?"
- Fast enumerate: stat + size + mtime, no full reads.
- Byte-range fetch: for header / preview extraction.
- Offline reporting: mark asset `originalAvailable=false`, keep thumb/browse.

## The "never read the full file" ingest pipeline

For each new file, bytes are read in this order, stopping as soon as we have
what we need:

1. **Stat only** — path, size, mtime → dedup against catalog. If unchanged, skip.
2. **Header (~256 KB)** — EXIF/XMP/GPS/codec via exiftool. One range read, not the whole file.
3. **Embedded preview** — every format we care about has one:
   - **RAW** (CR3/ARW/NEF/DNG): JPEG preview inside, extractable without RAW decode.
   - **HEIC / iPhone**: embedded thumbnail.
   - **MP4 / MOV** (iPhone, ProRes, DJI): moov atom + first GOP = poster frame.
   - **Insta360 `.insv`**: adjacent `.lrv` already on the card. Use it.
   - **DJI drone**: `.LRF` proxy + `.SRT` telemetry.
4. **Proxy generation** — only if no usable embedded preview. ffmpeg with range
   reads → 1080p H.264 proxy + poster frame. Cached forever on tier-0.
5. **AI enrichment runs on proxies, not originals** — CLIP on the poster,
   Whisper on the proxy's audio, captioner on the poster. Means you never send
   a 40 GB ProRes through ML.

Identity check without full hashing: SHA-256 of the **last 1 MB** as a cheap
fingerprint; full-file hash only on explicit dedup sweeps.

## Face recognition pipeline

- Stock model: InsightFace `buffalo_l` (accuracy-first). `buffalo_s` as a
  fallback if we want speed.
- Detection → embedding (512-d) → DBSCAN cluster → user labels cluster once →
  new assets auto-attach by embedding similarity.
- Stored in Postgres (catalog + embedding vectors via pgvector).
- On the Mac: `immich-ml-metal` runs **Apple Vision** for detection (on ANE,
  essentially free) and **CoreML ArcFace** for embeddings (10–50× vs Ryzen R1600).
- First backfill: run on the Mac. 50k photos ≈ 1 hour.
- Steady state: new photos embed in seconds either on Mac or Syno CPU fallback.

## AI enrichment

All enrichment workers share a pattern: they pull a job from a Postgres-backed
queue keyed by asset checksum, process the proxy/poster, and write results back
via Immich REST. Idempotent — re-running is a no-op.

| Worker | Input | Output | Backend |
|---|---|---|---|
| CLIP embedder | poster frame | 768-d vector → smart-search | MLX on Mac, ONNX on Syno |
| Whisper | proxy audio track | `.srt` sidecar + description | `whisper.cpp` Metal on Mac |
| Captioner | poster frame | description prefix "AI: …" | `moondream2` / BLIP-2 on Mac |
| Face detect + embed | poster frame | faces table | Apple Vision + ArcFace on Mac, InsightFace on Syno |

## Event clustering

- Nightly cron on the Mac or Syno.
- Pulls all assets via `/api/search/metadata`.
- DBSCAN on `(unix_timestamp_scaled, lat, lon)` with ε tuned so that typical
  trips become single clusters (≈ 6 h time, ≈ 30 km space).
- For each cluster: reverse-geocode centroid via Nominatim → album name
  (e.g. `2026-04 Lisbon`).
- Create/update album via `POST /api/albums`, `PUT /api/albums/:id/assets`.
- Skip singletons and trivially small clusters.

## Metadata gap-fill

Tiny web app served by the sidecar, not a fork of Immich's UI.

- Query: assets with `gps IS NULL` or `timestamp IS NULL` in the last N days,
  grouped by nearest temporal-spatial cluster.
- For each group: show thumb grid + suggested location (from nearest
  timestamped neighbour) + "apply to all N" button.
- Writes via `PUT /api/assets/:id` with EXIF GPS tags; also updates the XMP
  sidecar file so the originals carry the info if moved elsewhere.

## Ghost assets

When an external volume unmounts:
- Asset row keeps `status=offline`.
- Thumbs, proxies, transcripts, embeddings, captions, face data remain on tier-0.
- Timeline, search, CLIP, face-search all still work.
- "Open original" returns a helpful error: "volume 'archive-2024' is offline".
- On remount, assets auto-resume.

Unique advantage vs stock Immich: 20 TB of archive drives stay searchable.

## Queues and idempotency

- Every worker is stateless. State is in Postgres.
- Jobs keyed by `(asset_checksum, worker_name, worker_version)`.
- Workers can die, drives can unmount, the Mac can sleep — on resume the queue
  picks up where it left off.
- Mac unavailable? Workers that require Metal pause; Syno CPU workers
  (Immich stock ML) continue at lower throughput. No lost jobs.

## Sync to Immich

Only three API surfaces:
- `POST /api/libraries/:id/scan` — kick external-library rescan after inbox move.
- `PUT /api/assets/:id` — description, tags, GPS.
- `POST /api/albums`, `POST /api/albums/:id/assets` — event albums.

Deliberately minimal. If Immich changes the API, we fix a thin client, not a
sprawling integration.

## What NOT to build

- A new web UI. Immich's is better than anything we'd write.
- A new DB schema. Use Immich's tables + pgvector.
- A new mobile app. Use Immich's iOS/Android apps.
- A file manager. The NAS already is one.
