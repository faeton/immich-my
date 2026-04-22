# Offline runbook — `immy` with a local VLM

This document is written so a local LLM agent (Gemma, Qwen, Llama, etc.)
can read it and drive `immy` without internet access. Every command
below runs against services on `localhost` only — LM Studio for the
VLM, Postgres for Immich state, the local filesystem for photos.

## 1. Prerequisites (one-time, online)

Before going fully offline, make sure these are present on the machine:

- `uv`-managed venv at `~/Sites/immich-my/immy/.venv` with all deps
  installed (`cd ~/Sites/immich-my/immy && uv sync`).
- `~/.cache/huggingface/hub/` contains the Whisper model
  (`mlx-community/whisper-large-v3-mlx`). Pulled on first
  `--with-transcripts` run.
- LM Studio installed with at least one vision-capable model fully
  downloaded. Confirmed working picks:
  - `google/gemma-4-26b-a4b` (Q4_K_M GGUF, ~18 GB) — best quality,
    reasoning model, ~9.5 s/image.
  - `qwen2.5-vl-7b-instruct` — faster non-reasoning alternative,
    ~3 s/image, less detailed.
- `~/.immy/config.yml` populated with `pg:`, `media:`, `ml:` sections.
  Template below.
- One-time `immy db-setup` run against the Immich DB to create the
  trigram GIN index on `asset_exif.description`. Without it, every
  caption/transcript text search in the Immich UI does a sequential
  scan of `asset_exif`, which starts to hurt past ~50 k assets. The
  command is idempotent (`CREATE INDEX IF NOT EXISTS`); run it once
  per database.

- `~/.immy/library.yml` populated automatically on any online `immy
  process` run (caches `ownerId` + `importPaths[0]` so offline mode
  can anchor asset rows without a DB round-trip). If absent, `immy
  process --offline` falls back to reading `.audit/y_processed.yml`
  markers from a sibling trip to recover `container_root`; owner and
  library UUIDs are stamped as placeholders and resolved from the live
  DB at `immy sync-offline` time.

Once these exist, all further work is offline.

### 1a. True offline: `--offline` + `sync-offline`

"Offline" now means *no tailnet either*. The classic path (`immy
process`) writes directly to Postgres on the NAS — that needs the
tailnet up. For a plane / cafe / hotel wifi without tailnet:

```
# caches all asset + CLIP + faces + transcripts + caption data to
# <trip>/.audit/offline/<checksum>.yml (and sidecar .npy / .jsonl for
# embeddings). No network traffic except the local VLM on :1234.
immy process <trip> --offline --with-derivatives --with-clip \
    --with-faces --with-transcripts --with-captions
```

Later, when the tailnet / NAS is reachable again:

```
immy sync-offline <trip>   # tiny SQL traffic; replays the cache
```

`sync-offline` is idempotent (each entry marked `synced: true` after
success), so you can run it multiple times without duplicates. It also
re-caches `~/.immy/library.yml` so future offline runs inherit real
owner/library UUIDs instead of placeholders.

The batch script `tools/caption-all-trips.sh` defaults to `--offline`;
pass `--online` when the tailnet is up, or `--sync` to just replay.

## 2. Start the local VLM server

In LM Studio:

1. Load the model (Developer tab → pick model → Load).
2. Set **Context Length 8192**, **GPU Offload max**.
3. Developer tab → **Start Server**. Default bind: `http://localhost:1234`.

Smoke-test:

```
curl -s http://localhost:1234/v1/models | python -m json.tool
```

Expect a JSON list with the loaded model id. The id is what goes into
`ml.captioner.model`.

## 3. Config template

`~/.immy/config.yml` — edit values in `<angle brackets>`:

```yaml
originals_root: /Volumes/<nas-share>/originals
pg:
  host: 127.0.0.1
  port: 15432
  user: postgres
  password: <password>
  database: immich
media:
  host_root: /Volumes/<nas-share>/library
  container_root: /data
ml:
  clip_model: ViT-B-32__openai
  whisper_prompt: "English, Russian, Ukrainian."
  captioner:
    endpoint: http://localhost:1234/v1
    model: google/gemma-4-26b-a4b
    prompt: "Describe this photo in one short sentence. Focus on subjects, setting, and any visible text. Answer directly; do not think step by step."
    max_tokens: 1024
```

## 4. Run the test suite (sanity)

Before touching real trips, confirm the code still works end-to-end:

```
cd ~/Sites/immich-my/immy
.venv/bin/python -m pytest tests/ -x -q
```

Expect `201 passed` (or higher as tests are added). The captioner tests
mock the HTTP call — they do not need LM Studio running.

## 5. Process a trip folder

Replace `<trip>` with a subfolder of `~/Media/Trips/`, e.g.
`2026-04-bolivia-death-valley`.

### Dry-run — scan only, no DB writes

```
.venv/bin/immy audit ~/Media/Trips/<trip>
```

Produces `~/Media/Trips/<trip>/.audit/audit.yml`. No DB, no Whisper, no
VLM. Use this to confirm file-count and EXIF extraction are sane.

### Full pipeline — ingest + derivatives + CLIP + faces + transcripts + captions

```
.venv/bin/immy process ~/Media/Trips/<trip> \
  --with-derivatives \
  --with-clip \
  --with-faces \
  --with-transcripts \
  --with-captions
```

Flags are idempotent: safe to re-run after interruption. Each phase
self-skips on assets already done.

### Captions only — on an already-processed trip

```
.venv/bin/immy process ~/Media/Trips/<trip> --with-captions --no-derivatives --no-clip --no-faces
```

The captioner reads `asset_exif.description` first. If a row has
user-typed text (no `AI: ` prefix), the VLM is not called at all — no
token cost, no latency. Only empty or previously AI-captioned rows get
touched.

## 6. Check progress / results

After a run, inspect the marker:

```
cat ~/Media/Trips/<trip>/.audit/process.yml
```

Look for:

- `inserted`, `already_present` — new vs reused assets.
- `captions_written` — number of photos the VLM captioned this run.
- `assets[].caption.text` — per-photo captions with model + token counts.

## 7. Expected throughput (Apple Silicon MacBook)

| Pass                   | Rate / asset        | 1 000 photos | 20 000 photos |
|------------------------|---------------------|--------------|---------------|
| audit (EXIF only)      | ~20 ms              | 20 s         | 7 min         |
| derivatives            | ~0.5 s              | 8 min        | 3 h           |
| CLIP embed             | ~0.1 s              | 2 min        | 30 min        |
| faces (InsightFace)    | ~0.3 s              | 5 min        | 1.5 h         |
| Whisper (video only)   | ~1× realtime on audio | depends on video length |
| captions (Gemma 4)     | ~9.5 s              | 160 min      | **53 h**      |
| captions (Qwen2.5-VL-7B)| ~3 s               | 50 min       | 17 h          |

Captions dominate wall time. Plan overnight, on AC power, with
`caffeinate`:

```
caffeinate -dims .venv/bin/immy process ~/Media/Trips/<trip> --with-captions
```

## 8. Interrupt + resume

`Ctrl-C` is safe. The SQL UPDATEs for captions are gated:

```sql
WHERE description IS NULL OR description = '' OR description LIKE 'AI: %'
```

— so a re-run picks up where it stopped. User-typed descriptions are
never overwritten even if you re-run with a different model.

## 9. Swap captioner model mid-project

```
IMMY_CAPTIONER_MODEL=qwen2.5-vl-7b-instruct \
  .venv/bin/immy process ~/Media/Trips/<trip> --with-captions
```

Env vars win over `config.yml`. Supported overrides:

- `IMMY_CAPTIONER_ENDPOINT`
- `IMMY_CAPTIONER_MODEL`
- `IMMY_CAPTIONER_API_KEY_ENV` (name of an env var holding the key)
- `IMMY_CAPTIONER_PROMPT`
- `IMMY_CAPTIONER_MAX_TOKENS`
- `IMMY_WHISPER_PROMPT`

## 10. Troubleshooting

- **`empty caption in model response`** — the VLM is a reasoning model
  and used all `max_tokens` on hidden reasoning_content. Raise
  `max_tokens` in config.yml to ≥ 1024.
- **`connection to http://localhost:1234/v1 failed`** — LM Studio's
  server is not running. Start it in the Developer tab.
- **`HTTP 400 … unknown model`** — the `model` string in config does
  not match any loaded model. Query `curl http://localhost:1234/v1/models`.
- **`HTTP 500 … prompt exceeds context`** — bump Context Length in
  LM Studio's load dialog to 8192 or 16384.
- **Postgres connection refused** — Immich's compose stack isn't
  running on the NAS, or the SSH tunnel to port 15432 is down.
