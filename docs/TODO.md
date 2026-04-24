# TODO

Explicit backlog for work that is **not shipped yet**.

Use this file as the quick "what's next / what still matters" list.
Use [PLAN.md](./PLAN.md) for the longer phased build narrative and acceptance
criteria.

## Active

### Phase 2c residuals

- [x] `immy bloat sample <folder>` — before/after review for candidate
  transcodes. Extracts matched frames from every `*.optimized.*` /
  source pair at evenly-spaced percentages (default 10/30/50/70/90 %),
  runs ffmpeg's `psnr` filter on the full pair, renders
  `<folder>/.audit/bloat-review/review.md` with inline JPEG thumbs and
  per-file verdict (`ok` ≥ 30 dB, `review` ≥ 25 dB, `fail` below).
  Non-destructive — run before `bloat transcode … --apply`.

Remaining:
- Catalog identity guarantees after `--apply` on real libraries — this
  is an audit task (verify asset row + derivatives + library re-scan
  handle in-place replacement) rather than a build task. Open when
  something actually breaks; the current `_verify` check on duration
  + stream count has held across every transcode run so far.

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

- [x] Transcript / caption search integration
  - `POST /api/search/metadata { description: "…" }` matches
    case-insensitively against `asset_exif.description` end-to-end —
    works for both Whisper transcripts and VLM captions once they
    reach the DB.
  - `immy db-setup` creates `immy_idx_asset_exif_description_trigram`
    (GIN / `f_unaccent(description) gin_trgm_ops`), matching Immich's
    own pattern for filename + place-name indexes. Idempotent.
  - Docs: `docs/CAPTIONS.md` → "Making captions searchable",
    `docs/OFFLINE-RUNBOOK.md` prereq #5.

- [x] Job queue + resumability for enrichment workers
  - per-trip journal at `.audit/journal.yml`, keyed by
    `(checksum_hex, worker, version)` — workers are `ingest`,
    `derivatives`, `clip`, `faces`, `transcript`, `caption`
  - online path now commits **per asset** (not per trip), so a Ctrl-C
    mid-trip leaves every completed asset durable
  - on resume, each phase checks the journal at the current model
    version and short-circuits if done; derivatives also re-verify
    that staged files exist on disk before trusting the journal
  - `--recaption` ignores the journal so the user can force a re-run
  - version strings are `clip:<model>`, `caption:<model>`, etc., so
    bumping a model invalidates only that worker's entries

### Phase 7 — External library matching

- [x] `immy snapshot` — dump Immich library index (asset_id, filename,
  size, SHA1, taken_at, type, library_id) to `~/.immy/library-snapshot.sqlite`.
  Read-only on Immich; uses a server-side cursor so 100k+ libraries stream
  without buffering. Checksum stored as raw 20-byte BLOB (~40% vs base64).
- [x] `immy find-duplicates <path>` — walk any directory tree, classify
  every file as `exact` / `likely` / `name-only` / `no-match` against the
  snapshot. Default mode hashes only when `(name, size)` already matched —
  keeps terabytes from being read for nothing. `--fast` skips hashing
  entirely; `--thorough` hashes everything and catches pure renames via
  checksum lookup. Skips macOS bundles by default; `--into-bundles`
  descends. Writes `dupes.md` (human) + `dupes.json` (scripting).

- [x] `immy apple-people` — **dry-run only**. Read-only reader for
  `Photos.sqlite` (resolves `ZMERGETARGETPERSON` chains, pulls real
  filename+size from `ZADDITIONALASSETATTRIBUTES`), prints per-person
  match rate against the Immich snapshot by `(filename, size)`. No
  writes yet — the `--apply` path that creates Immich Person rows and
  attaches face embeddings is the remaining Phase 7 work.

Not shipped yet (build spec in [/PLAN.md](../PLAN.md)):
- `immy apple-people --apply` — write path: `POST /api/people` per named
  person, `UPDATE asset_faces SET personId` where bbox-IoU(apple) > 0.3.
  Deferred until dry-run match rate is validated on a fresh snapshot.
  Open questions before writing:
  - Immich `asset_faces` bbox coord system — normalized 0..1 or pixels?
    Inspect schema on live DB before committing to IoU math.
  - Snapshot schema v1 doesn't carry width/height; we'd need v2 to
    convert Apple normalized bboxes into Immich pixel space.
  - Two *already-named* Apple persons with the same `ZFULLNAME` would
    double-POST to `/api/people` — de-dupe by full_name in `apply()`.
  - Rate-limit `POST /api/people` to keep Immich's job queue sane when
    seeding 50+ names at once.
- `immy find-similar` — CLIP near-dup finder for re-exports / edits /
  crops that broke byte-identity. Needs CLIP embeddings in the snapshot.

### Phase 4 — Event clustering

- [x] `immy cluster` — sweep-based clustering on `(dateTimeOriginal,
  latitude, longitude)` from `asset_exif`; new event when time gap
  > 4 h OR distance-from-centroid > 5 km (both tunable via CLI).
  Singletons/doubletons dropped by default (`--min-assets`).
- [x] Album naming from Immich's own `city` / `country` (populated by
  its reverse-geocode worker). Dominant-city wins ties. Date format is
  locale-independent and sortable (`19 Feb 2024`, `10–12 Jul 2024`,
  `29 Apr – 3 May 2024`, `31 Dec 2024 – 2 Jan 2025`).
- [x] Idempotent album create/update via a `immy-cluster:<key>` marker
  line embedded in each album's description. Stable key derived from
  rounded centroid + start date so late-arriving photos don't spawn
  duplicates.

Known MVP limitation:
- We only *add* assets to existing immy-cluster albums. If an asset's
  cluster membership changes between runs (e.g. you refined EXIF
  timestamps), the asset ends up in both the old and new album. Prune
  manually when that happens. A proper "remove stale memberships" pass
  needs a separate mapping of asset → prior-cluster-key. The Phase 3
  journal could carry that mapping under a `cluster` worker key, but
  the wire-up is still TODO.

### Pipeline optimizations

Shipped:
- `immy process TRIP1 TRIP2 …` — MLX/InsightFace/Whisper load once per
  batch instead of per-trip invocation. Per-trip commit boundary keeps
  earlier trips durable through Ctrl-C / a later-trip failure.
- Caption resume on the online path — skip the VLM when
  `asset_exif.description` is already AI-prefixed. `--recaption` forces.
- `tools/process-all-trips.sh --status` surfaces partial progress from
  `.audit/journal.yml` when `.audit/process.yml` hasn't been written yet
  (interrupted batch). Earlier the table labelled every interrupted trip
  "never processed" and hid hours of per-asset work.
- DNG embedded-preview fast path — `exiftool -b -JpgFromRaw` /
  `-PreviewImage` into libvips. ~3.5× on DJI DNG derivatives vs going
  through libraw's init on every call.

Deferred:
- Batched CLIP + face embeddings — collect previews trip-wide, run MLX
  CLIP and ArcFace in batches of ~16. Would be 2–3× on CLIP/faces, but
  those together are <5 % of overnight wall time once captions are on
  (Gemma ≈ 9.5 s/image dominates). Worth revisiting if we move
  captioning off-device (cloud VLMs are ~0.5 s/image) or run a
  caption-off pipeline at scale.

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

## Notes

- "Shipped" means implemented in `immy` and tested.
- If something is only described in architecture docs but missing here, add it.
- If something lands, remove it from here and keep the implementation detail in
  [PLAN.md](./PLAN.md) and the relevant code/docs.
