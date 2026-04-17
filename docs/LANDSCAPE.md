# Landscape: open-source self-hosted media management (April 2026)

Survey done before committing to a base. Verdict at the bottom.

## Comparison

Legend: ✅ native, ~ partial, ❌ absent.

| Tool | Ingest watch | EXIF | pHash dupes | CLIP search | Faces | Captions / Whisper | Event auto-group | Web UI | iCloud / Photos.app | Activity |
|------|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| **Immich** | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ (stacks only) | excellent | iOS app | daily commits |
| PhotoPrism | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | good | ❌ | weekly |
| LibrePhotos | ✅ | ✅ | ~ | ~ | ✅ | ✅ (BLIP) | ✅ | slow | ❌ | sporadic |
| Ente (self-host) | app-only | ✅ | ~ | ✅ | ✅ | ❌ | ❌ | polished | ✅ | active |
| digiKam | — (desktop) | ✅✅ | ✅ | ❌ | ✅ | ❌ | ❌ | no web | ❌ | active |
| Damselfly | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ok | ❌ | active |
| Nextcloud Memories | via NC | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ok | ❌ | active |
| Photoview | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ok | ❌ | stale |
| Lychee / Piwigo | ✅ | ✅ | ❌ | ❌ | ~ | ❌ | ❌ | varies | ❌ | active |

Notable: only **LibrePhotos** ships event clustering + captions today, but its
UI and release cadence trail Immich materially. Only **Ente** has a clean
Apple/iCloud import story, but it's encryption-first which makes bolt-ons
harder.

## Why Immich wins as the base

- Modern Svelte UI with virtualised timeline and keyboard nav — the only one
  that feels "Google-Photos-fast" in the browser.
- First-class iOS app for auto-upload from the camera roll.
- CLIP smart search, face recognition (InsightFace `buffalo_l`), pHash
  duplicate detection all built in.
- External libraries allow read-only mounts — fits the remote-storage pattern
  directly.
- Public REST API is stable enough to drive sidecar enrichment cleanly.
- Actively maintained, daily commits, large community, weekly releases.

Sources:
- [Immich docs](https://immich.app/docs)
- [Immich smart search / CLIP](https://immich.app/docs/features/smart-search)
- [Immich duplicate detection](https://immich.app/docs/features/duplicate-detection/)
- [Immich facial recognition models discussion](https://github.com/immich-app/immich/discussions/4081)

## Gaps Immich still has (and what fills them)

| Gap | How we fill it |
|---|---|
| Whisper transcripts | `whisper.cpp` / `faster-whisper` in sidecar, result → `asset.description` via API |
| AI captions for search | `moondream2` or BLIP-2 in sidecar, appended to description |
| Event / trip clustering | DBSCAN on `(time, lat, lon)` → albums via `POST /api/albums` |
| 360 `.insv/.insp` support | `telemetry-parser` + Insta360 CLI; harvest `.lrv` proxies |
| DJI telemetry | `dji-srt-parser` → XMP sidecar |
| Apple Photos library import | [`osxphotos`](https://github.com/RhetTbull/osxphotos) + `icloudpd` |
| Metadata-gap bulk fill | Small sidecar UI grouping assets missing GPS/time |
| Offline / ghost volumes | Mount adapter layer with tombstone + resurrection states |

## Auxiliary OSS tools worth pinning

- **exiftool** — metadata extraction and XMP writing.
- **Czkawka / rdfind / jdupes / dupeGuru** — near-dupe finders (complement
  Immich's built-in pHash for cross-device bursts).
- **whisper.cpp / faster-whisper** — video audio transcription.
- **MLX / llama.cpp** — on-Mac inference; used by `immich-ml-metal`.
- **Self-hosted Nominatim** — reverse geocoding (Immich already uses this).
- **osxphotos** — reads the Photos.app SQLite and exports originals with
  full sidecars including people names and keywords.
- **icloudpd** — pulls iCloud-only items.
- **telemetry-parser** / **dji-srt-parser** / **gyroflow** — drone/360 telemetry.

## Community forks that matter

- [`immich-ml-metal`](https://github.com/sebastianfredette/immich-ml-metal) —
  drop-in ML replacement using Metal / ANE / CoreML. Apple Vision face detect,
  MLX CLIP, InsightFace ArcFace via CoreML.
- [`immich-apple-silicon`](https://github.com/epheterson/immich-apple-silicon) —
  broader Apple Silicon optimisation: ML + VideoToolbox + Core Image thumbs.
- [`immich_ml_balancer`](https://github.com/apetersson/immich_ml_balancer) —
  load-balance multiple ML workers, so the Mac can be a burst accelerator
  while the Syno CPU worker is the always-on fallback.

These are unofficial but real and actively used. Low risk because they're
stateless HTTP workers.

## Build vs extend

Do not build from scratch. Immich's ingest + DB + UI + mobile stack alone is
multi-year work. Extend it via a sidecar that speaks REST. Contribute event
clustering upstream if we get it working — it's a frequently-requested
feature.
