# TODO

Explicit backlog for work that is **not shipped yet**.

Use this file as the quick "what's next / what still matters" list.
Use [PLAN.md](./PLAN.md) for the longer phased build narrative and acceptance
criteria.

## Active

### Phase 2c residuals

- Improve bloat/transcode UX beyond the current CLI flow.
- Add better sampling / before-after review for candidate transcodes.
- Tighten catalog identity guarantees after `--apply` on real libraries.

### Phase 3 — Proxy-first AI enrichment

- [x] Whisper transcripts for videos — `immy process --with-transcripts`
  - mlx-whisper large-v3 on Apple Silicon
  - writes `<stem>.<lang>.srt` next to the source (compound suffix keeps
    it clear of DJI telemetry `.SRT` siblings)
  - excerpt (first ~500 chars) goes into `asset_exif.description` so
    spoken words are findable in Immich search
  - four-gate fast path (sidecar cache / EXIF-make denylist / ffprobe
    audio-stream / ffmpeg volumedetect) — silent drone footage skips
    Whisper entirely in <300 ms
  - `ml.whisper_prompt` in config or `IMMY_WHISPER_PROMPT` env var biases
    auto-detect toward priority languages (e.g. `"English, Russian,
    Ukrainian."`) — passed as Whisper `initial_prompt`

- [x] Image captioner — `immy process --with-captions`
  - any OpenAI-compat endpoint (LM Studio / Ollama / OpenAI / Anthropic
    compat / Gemini compat / OpenRouter / Groq) — one config-only swap
  - feeds the 1440 px `preview.jpeg` when staged; pyvips-resizes the
    original in memory otherwise
  - writes `AI: <sentence>` into `asset_exif.description`; pre-reads
    existing description to skip the paid call when user text is
    already there; SQL UPDATE is gated `LIKE 'AI: %'` so user text can
    never be clobbered even under races
  - records model + token counts per image in `.audit/process.yml` for
    post-hoc cost audit
  - see [CAPTIONS.md](CAPTIONS.md) for the price-per-1k-images table

- [x] True offline mode — `immy process --offline` + `immy sync-offline`
  - caches asset + exif + CLIP embedding + face embeddings + caption
    text to `<trip>/.audit/offline/<checksum>.yml` (+ sibling `.npy` /
    `.jsonl` for embeddings) when Postgres is unreachable
  - idempotent re-runs: checksum-keyed entries reuse asset UUIDs
  - `immy sync-offline <trip>` replays the cache into DB; each entry
    runs in its own transaction, placeholder owner/library UUIDs get
    swapped for real values fetched at sync time
  - `~/.immy/library.yml` is cached after every online run so offline
    mode has real UUIDs when available; absent it, `container_root` is
    recovered from an existing `y_processed.yml` marker

Not shipped yet.
- Transcript / caption search integration
  - searchable without needing the original file online
- Job queue + resumability for enrichment workers
  - keyed by `(checksum, worker, version)`
  - safe to resume after crash / sleep / disconnect

### Phase 4 — Event clustering

Not shipped yet.

- Nightly clustering on `(time, lat, lon)`
- Album naming from reverse geocoding
- Idempotent album create/update flow

## Planned

### Phase 5 — Metadata gap-fill UI

- Small sidecar web UI for missing GPS / timestamp groups
- Group-level apply flow instead of per-asset edits
- Keep XMP sidecars and Immich metadata in sync

### Phase 6 — Ghost assets

- Keep offline originals searchable
- Friendly offline/original-unavailable state
- Automatic resurrection on remount

### Quality of life

- Cross-device near-duplicate reporting
- Export-to-edit workflows
- Backup automation
- Apple Photos people-name seeding

## Notes

- "Shipped" means implemented in `immy` and tested.
- If something is only described in architecture docs but missing here, add it.
- If something lands, remove it from here and keep the implementation detail in
  [PLAN.md](./PLAN.md) and the relevant code/docs.
