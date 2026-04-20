# immy

Pre-ingest metadata forensics for trip folders bound for Immich. See
[../docs/SIDECAR.md](../docs/SIDECAR.md) for the full design and
[../docs/PLAN.md](../docs/PLAN.md) Phase 2a for the iteration ladder.

## Dev

```sh
uv sync
uv run immy --help
uv run immy audit tests/fixtures/dji-srt-pair
uv run pytest
```

Requires `exiftool` and `ffmpeg`/`ffprobe` on PATH
(`brew install exiftool ffmpeg`).

## Commands

| Command | What it does |
|---|---|
| `immy audit <folder>` | Forensic report — EXIF/XMP/sidecars/durations, no writes |
| `immy bloat <folder>` | Group oversized sources by folder; confirm + re-encode |
| `immy process <folder>` | **Phase Y** — insert asset + EXIF + derivatives (thumb/preview/encoded_video) + CLIP + faces straight into Immich Postgres |
| `immy promote <folder>` | rsync originals + staged derivatives to the NAS, UPSERT `asset_file` rows, create/update album |

`process` flags: `--with-derivatives/--no-derivatives`,
`--with-clip/--no-clip`, `--with-faces/--no-faces`,
`--transcode/--no-transcode` (all default on). Drops
`.audit/y_processed.yml` so `promote` skips the scan POST.

Config lives at `~/.immy/config.yml` — see
[../docs/DEPLOY.md](../docs/DEPLOY.md) for `pg:`, `immich:`, `media:`,
`ml:` block shapes.
