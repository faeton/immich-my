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

Requires `exiftool` on PATH (`brew install exiftool`).
