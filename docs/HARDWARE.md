# Hardware

Verified specs and what runs where, for the specific boxes we have.

## Synology DS923+ (storage + always-on core)

| Property | Value | Source |
|---|---|---|
| CPU | AMD Ryzen R1600, **2 cores / 4 threads**, 2.6 GHz base, 3.1 GHz boost | [Synology datasheet (PDF)](https://global.download.synology.com/download/Document/Hardware/DataSheet/DiskStation/23-year/DS923+/enu/Synology_DS923+_Data_Sheet_enu.pdf), [blackvoid review](https://www.blackvoid.club/synology-ds923-review/) |
| iGPU | **None** (no QuickSync / no VAAPI) | same |
| RAM (ours) | 20 GB ECC DDR4 SODIMM (official ceiling 32 GB) | same |
| Drive bays | 4× 3.5"/2.5" HDD/SSD | same |
| NVMe | 2× M.2 2280, **usable as storage pool on DSM 7.2+** | [DSM 7.2 NVMe pool](https://www.blackvoid.club/synology-ds923-review/) |
| Network | 2× 1 GbE, LAG-capable; optional 10 GbE via **E10G22-T1-Mini** | same |
| Expansion | eSATA for DX517 (5-bay) | same |
| DSM | 7.2+ (predates 2025 NVMe lock-in — any compatible NVMe works) | [drive compat KB](https://kb.synology.com/en-us/DSM/tutorial/Drive_compatibility_policies) |

**Verdict**: great as always-on storage + Immich web/DB host. Bad as an ML box:
2c/4t and no iGPU means Whisper / CLIP / BLIP on CPU are slow. Use remote ML.

## MacBook Apple Silicon (burst compute)

- Runs the curator sidecar + `immich-ml-metal` via OrbStack or Docker Desktop.
- Apple Silicon gives us:
  - **Neural Engine** (16-core on M1/M2, 16+ on M3/M4) for face detection via
    Apple Vision and ArcFace via CoreML.
  - **Metal GPU** for MLX CLIP embeddings and `whisper.cpp`.
  - **VideoToolbox** for hardware-accelerated ProRes/HEVC/H.264 transcoding —
    the single biggest perf gap vs the Syno.
- Expected speedup vs DS923+: **10–50×** on face embedding, **near-realtime**
  face detection, hours → minutes for large Whisper batches. Community report:
  MacBook M1 ≈ 35× a Synology DS918+ for ML. ([NAS LLM comparison](https://needtoknowit.com.au/blog/best-nas-for-local-llm/))
- Sleeps are fine — queues drain on wake.

## What runs where

| Component | DS923+ | MacBook | Notes |
|---|:-:|:-:|---|
| Immich server (API + web) | ✅ | | always-on |
| Postgres + pgvector | ✅ | | NVMe volume, CoW disabled |
| Redis | ✅ | | |
| Immich stock ML (CPU, fallback) | ✅ | | keeps faces/CLIP alive when Mac asleep |
| Nominatim | ✅ | | self-hosted reverse geocoder |
| `immich-ml-metal` | | ✅ | ANE + CoreML + MLX |
| Curator sidecar | | ✅ | workers queue on NAS, execute on Mac |
| Whisper (video transcription) | | ✅ | `whisper.cpp` Metal |
| Captioner (moondream/BLIP) | | ✅ | MLX or CoreML |
| Video proxy generation | | ✅ | VideoToolbox |
| 360 stitching | | ✅ | Insta360 CLI, telemetry-parser |
| `osxphotos` Photos.app pull | | ✅ | needs Full Disk Access |
| `icloudpd` iCloud pull | | ✅ | uses the Apple ID keychain |
| Reverse-proxy / HTTPS | ✅ | | DSM's built-in Nginx + Let's Encrypt |
| Hyper Backup | ✅ | | DB + library backups |

## NVMe layout on the Syno

Create a separate NVMe storage volume (not a cache):

```
/volumeNVMe/
├── immich/
│   ├── thumbs/
│   ├── proxies/
│   ├── transcripts/
│   └── captions/
└── postgres/          # chattr +C to disable BTRFS CoW on the DB dir
```

Why NVMe matters: thumb scrolling over 100k+ assets is IO-bound. Postgres on
HDD makes the Immich UI feel sluggish; on NVMe it's snappy.

## Network

- 2× 1 GbE in **LAG / SMB Multichannel** doubles practical throughput to ~220
  MB/s for a single Mac client. Good enough for browsing proxies and
  reasonable for editing H.264 originals.
- If you edit **ProRes or RAW video** over the mount, upgrade to **10 GbE**
  via the `E10G22-T1-Mini` card (~€170). The Mac needs a Thunderbolt 10 GbE
  adapter or a 10 GbE-equipped Mac Studio / mini.

## Performance expectations

All rough personal-library scale.

| Task | DS923+ alone | DS923+ + MacBook | Notes |
|---|---|---|---|
| Face backfill, 50k photos | 1–3 days | ~1 hour | ANE face detection is basically free |
| CLIP embedding backfill, 50k | ~half a day | ~20 minutes | MLX CLIP on M-series |
| Whisper on 10 hours of video | ~2 days CPU | ~30 minutes Metal | `whisper.cpp` w/ Metal |
| ProRes → 1080p H.264 proxy, 1 hr clip | ~3× realtime | ~0.2× realtime | VideoToolbox |
| Timeline scroll over 500k assets | 60 fps | 60 fps | Immich web UI, thumbs on NVMe |
| Ingest 10 GB SD card | minutes | minutes | dominated by copy, not processing |

## Not this box

Things the DS923+ + MacBook pairing does **not** do well, even with the split:

- **Always-on ML-heavy workloads** (e.g. bulk captioning 500k photos in one
  go). The Mac must be awake. If this bothers you, add a **Mac mini M4** or
  **N100 mini-PC** as a dedicated always-on compute node. Zero architecture
  change — just another ML worker behind the balancer.
- **GPU-accelerated anything on the Syno** — no PCIe, no iGPU, done.
- **Hardware video transcoding on the Syno** — CPU only. Fine for generating
  proxies once; bad for live transcode streams.

## Sources

- Synology DS923+ datasheet:
  <https://global.download.synology.com/download/Document/Hardware/DataSheet/DiskStation/23-year/DS923+/enu/Synology_DS923+_Data_Sheet_enu.pdf>
- blackvoid DS923+ review: <https://www.blackvoid.club/synology-ds923-review/>
- Immich on Synology (official): <https://docs.immich.app/install/synology/>
- Immich hardware transcoding: <https://docs.immich.app/features/hardware-transcoding/>
- Immich remote ML: <https://docs.immich.app/guides/remote-machine-learning/>
- Immich hardware-accelerated ML: <https://docs.immich.app/features/ml-hardware-acceleration/>
- `immich-ml-metal`: <https://github.com/sebastianfredette/immich-ml-metal>
- `immich-apple-silicon`: <https://github.com/epheterson/immich-apple-silicon>
- `immich_ml_balancer`: <https://github.com/apetersson/immich_ml_balancer>
- Drive compat FAQ (2025 models only; DS923+ unaffected):
  <https://kb.synology.com/en-us/DSM/tutorial/Drive_compatibility_policies>
