# Phase 3b — VLM image captions

`immy process --with-captions` runs a vision-language model over each
`IMAGE` asset, writes a short description into `asset_exif.description`
with an `AI: ` prefix, and records per-image token counts in the
`.audit/process.yml` marker so you can audit cost after the run.

Any OpenAI-compatible `/chat/completions` endpoint works:

| Backend            | URL                                         | Key env                |
|--------------------|---------------------------------------------|------------------------|
| LM Studio          | `http://localhost:1234/v1`                  | — (none)               |
| Ollama             | `http://localhost:11434/v1`                 | — (none)               |
| OpenAI             | `https://api.openai.com/v1`                 | `OPENAI_API_KEY`       |
| Anthropic (compat) | `https://api.anthropic.com/v1/`             | `ANTHROPIC_API_KEY`    |
| Google Gemini      | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY`       |
| OpenRouter         | `https://openrouter.ai/api/v1`              | `OPENROUTER_API_KEY`   |

## Config

`~/.immy/config.yml`:

```yaml
ml:
  clip_model: ViT-B-32__openai
  captioner:
    endpoint: http://localhost:1234/v1     # any OpenAI-compat URL
    model: qwen2.5-vl-7b-instruct          # whatever the backend exposes
    api_key_env: OPENAI_API_KEY            # env-var NAME, not the value
    prompt: "Describe this photo in one short sentence."
    max_tokens: 80
```

Per-run overrides via env:
- `IMMY_CAPTIONER_ENDPOINT`
- `IMMY_CAPTIONER_MODEL`
- `IMMY_CAPTIONER_API_KEY_ENV` (name of the env var holding the key)
- `IMMY_CAPTIONER_PROMPT`
- `IMMY_CAPTIONER_MAX_TOKENS`

Env wins over config.yml so the same config works across backends without
edits — swap `IMMY_CAPTIONER_ENDPOINT` / `IMMY_CAPTIONER_MODEL` per trip.

## Idempotency

- The captioner reads `asset_exif.description` first. If it holds a
  non-empty string that isn't `AI: …`, the API call is skipped — no
  token spend on photos with human-written descriptions or existing
  Whisper transcripts.
- On write, the UPDATE is gated `WHERE description IS NULL OR
  description = '' OR description LIKE 'AI: %'`. User-typed text cannot
  be clobbered even by a racing writer.
- Re-running with a different `model` upgrades existing AI captions in
  place. Re-running with the same `model` online: `immy process` skips
  photos that already have an `AI: …` description (cheapest possible
  resume). Pass `--recaption` to force regeneration. Offline mode uses
  a tighter guard — `prior_caption.model == config.model` — so model
  upgrades still fire.

## Making captions searchable

Captions go into `asset_exif.description`, which Immich's
`POST /api/search/metadata { "description": "…" }` already matches
case-insensitively. In the UI this is the "Description" text field in
the search pane.

At small scale that's fine, but `asset_exif` has no text index out of
the box. Run once per database:

```
immy db-setup
```

Creates `immy_idx_asset_exif_description_trigram` — a GIN trigram
index on `f_unaccent(description)` matching the pattern Immich uses
for filename and place-name search. The `immy_` prefix keeps it out
of Immich's migration namespace, so a future server upgrade can add a
similarly-named index without colliding. `CREATE INDEX IF NOT EXISTS`
makes re-runs a no-op.

**Captions only become searchable after they reach the DB.** If you
ran `immy process --offline`, captions live in
`<trip>/.audit/offline/<checksum>.yml` and are invisible to Immich
until `immy sync-offline <trip>` (or the `tools/caption-all-trips.sh
--sync` wrapper) pushes them.

## Cost per 1 000 images

Math assumes ~50-token prompt, ~80-token text output, a 1440×1080 JPEG
(what `derivatives.py` stages as `preview.jpeg` and what the captioner
feeds when available). Pricing is live as of 2026-04; re-check provider
pages before committing to a 20 k-photo trip.

| Provider / model              | Image token cost             | Per image | Per 1 000 |
|-------------------------------|------------------------------|-----------|-----------|
| Local (LM Studio / Ollama)    | —                            | $0        | **$0**    |
| Groq Llama-4-Scout vision     | ~1 000 in-tokens/img         | $0.00014  | **$0.14** |
| OpenAI gpt-4.1-mini (high)    | 765 in-tokens/img            | $0.00045  | **$0.45** |
| OpenAI gpt-4o-mini (low)      | 2 805 effective/img          | $0.00047  | **$0.47** |
| Google gemini-2.5-flash       | 1 032 in-tokens/img (tiled)  | $0.00053  | **$0.53** |
| Google gemini-2.5-pro         | 1 032 in-tokens/img          | $0.00215  | **$2.15** |
| Anthropic claude-haiku-4-5    | (w·h)/750 = 2 073 tokens/img | $0.00252  | **$2.52** |
| OpenAI gpt-4o (high)          | 765 in-tokens/img            | $0.00284  | **$2.84** |
| OpenAI gpt-4o-mini (high)     | ~25 k effective/img          | $0.00384  | **$3.84** |
| Anthropic claude-sonnet-4-6   | 2 073 in-tokens/img          | $0.00757  | **$7.57** |
| Anthropic claude-opus-4-7     | 2 073 in-tokens/img          | $0.01262  | **$12.62**|

For a typical trip (5–20 k photos):

- **Local (Qwen2.5-VL-7B on MLX)** — $0, ~3–5 s/image → 4–28 h wall.
  Fine overnight; matches the mobile-Mac constraint.
- **Local (Gemma 4 26B-A4B on LM Studio)** — $0, ~9–10 s/image on Apple
  Silicon. Higher-quality captions (reads in-frame text, brand names),
  but the model is reasoning-capable and spends ~300–900 tokens
  "thinking" before emitting each answer — raise `max_tokens` to ≥1024
  or it'll truncate. 20 k photos ≈ 50 h: plan for 2+ overnights or use
  a smaller model for bulk.
- **gemini-2.5-flash** — ~$3–10 per trip. Best $/quality for bulk.
- **gpt-4.1-mini** — ~$2–9 per trip. Similar tier.
- **claude-haiku-4-5** — ~$13–50 per trip. Richer captions, multilingual.
- **claude-sonnet-4-6** — ~$38–150 per trip. Reserve for photos you'd
  stand behind the caption of verbatim.

OpenRouter passes through upstream pricing + a 5.5 % one-time fee on
credit top-ups. Useful for A/B-testing models without juggling per-vendor
keys. Gemini and Groq both have free tiers generous enough for <1 k
photos/day test runs.

## Idempotent model upgrades

The `AI: ` prefix is stable across models. To swap models mid-project,
edit `captioner.model` in config.yml or pass `IMMY_CAPTIONER_MODEL=…`
and re-run `immy process --with-captions`. Only AI-captioned and empty
descriptions are overwritten; user text survives.

`.audit/process.yml` records the model name alongside each caption so
you can audit which images were captioned by which backend:

```yaml
assets:
  - container_path: /data/library/.../IMG_0123.jpg
    caption:
      text: "Two alpacas graze on a rocky hillside at sunset."
      model: claude-haiku-4-5
      prompt_tokens: 2123
      completion_tokens: 14
```
