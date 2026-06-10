# Transcripts: engines, hallucinations, verification

How video speech becomes `.srt` sidecars + searchable descriptions, what
goes wrong, and how the dual-engine quality check works. Companion to the
transcript phase in [SIDECAR.md](SIDECAR.md) and the engine table in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Pipeline (as shipped)

`immy process --with-transcripts` runs per video, in ascending cost order:

1. sidecar already on disk → reuse (idempotent re-runs pay nothing)
2. EXIF make on denylist (DJI — mute cameras) → skip
3. no audio stream (ffprobe) → skip
4. mean volume below −50 dB on a 5 s sample → skip
5. full-file silencedetect sweep, < 5 s of speech → skip
6. Whisper large-v3 (mlx) with anti-hallucination decode flags →
   `<stem>.<lang>.srt` + excerpt into `asset_exif.description` (empty-guard:
   never clobbers user text or an `AI: ` caption)

Anti-hallucination measures, validated by A/B on the worst loopers (2026-06):

- `condition_on_previous_text=False` — breaks the feedback loop where one
  hallucinated cue primes the next chunk into repeating it forever.
- `word_timestamps=True` + `hallucination_silence_threshold=2.0` — the
  decoder skips silent gaps when a window looks hallucinated.
- constrained language detect (`en`/`ru`/`uk`) — auto-detect on windy or
  near-silent clips otherwise locks onto `fo`/`nn`/`ja` and decodes garbage.
- `clip_timestamps` from the silencedetect speech timeline for long
  sparse-speech files — inference scales with speech, not duration.
- write-time scrub (`format_srt`): known boilerplate dropped per cue
  (`DimaTorzok` credits in any case/form, «Продолжение следует», subtitle
  credits), and decode loops (the same cue ≥ 6× consecutively) collapse to
  the first occurrence. If nothing survives the scrub the clip is journaled
  `whisper-hallucination` and no sidecar is written.

## Engine bench (2026-06-10)

28 files (8 worst hallucinators + 20 random), 3.1 h of mixed ru/en travel
audio, Apple M-series. RTFx = audio seconds per inference second.

| Engine | RTFx | Device | Boilerplate | Loops | Notes |
|---|---|---|---|---|---|
| Qwen3-ASR-1.7B (mlx-qwen3-asr) | 11.6 | GPU | none | 1 file (×4) | quality winner; best on mixed ru/en (keeps each language); occasionally flips a ru phrase to en; hallucinated Dutch once on a very noisy clip |
| Whisper large-v3 (pipeline) | 16.9 | GPU | 14/28 files | 6 files | boilerplate caught by write-time scrub, but the decoder still produces it; silently *translates* ru speech in en-detected files |
| GigaAM-v3 `v3_e2e_rnnt` | 39.2 | **CPU** | none | none | cleanest Russian, useless English; ideal second opinion |
| Canary-1B-v2 (NeMo) | 6.2 | CPU | 5/28 | catastrophic («Наконец» ×97) | eliminated; MPS rejects it (float64) |

Key-phrase probes: only Qwen and Whisper heard «Привет, бандит!»; Parakeet
v3 (tested separately) was structurally clean but less accurate.

Outcome: the 80 worst hallucinated files were re-transcribed with
Qwen3-ASR-1.7B (journal records `engine: mlx-qwen3-asr/...` in the
transcript meta; the version stays the pipeline default so coverage audits
stay green, and the worker's sidecar-exists gate keeps Whisper from ever
overwriting them). Pipeline default remains Whisper until a larger verified
sample justifies the swap.

Qwen integration notes:

- `load_model()` returns a `(model, config)` tuple.
- `transcribe(..., return_timestamps=True, return_chunks=True)`: the word
  stream has no punctuation; `chunks` (~17–25 s) carry punctuated text.
  SRT cues are built by sentence-splitting chunk text and consuming the
  word stream for timing (char-proportional fallback inside a chunk).

## Insta360 twins

Insta360 writes the same clip as two lens files (`VID_..._00_NNN` /
`VID_..._10_NNN`) plus an LRV proxy (`LRV_..._11_NNN.insv` or
`_01_NNN.lrv`) — identical audio, identical duration. In the worst-80
batch 54/80 files were twins: 6.7 of 15.1 audio-hours were duplicates.
Transcribe **once per group** (key: parent dir + datetime + clip number)
and fan the result out; `tools/verify-transcripts.py` verifies once per
group the same way.

## Dual-engine verification (`tools/verify-transcripts.py`)

Trust through agreement instead of faith in one model: every sidecar is
re-transcribed by an engine with unrelated failure modes (GigaAM for ru on
CPU, Whisper for en on GPU) and compared word-by-word
(`SequenceMatcher` over normalized words). On the worst-80 batch the
agreement median was 0.56; everything below 0.4 (16/59 unique tracks) went
to an LM Studio judge (`gemma-4-31b-it`).

Operational lessons baked into the tool:

- **GigaAM hard-fails above exactly 25.0 s** (400 000 samples). ffmpeg
  `-f segment -c copy` cuts at 25.024 s — every segment fails. Cut at 24 s
  re-encoded, and never swallow per-segment exceptions silently: the first
  buggy run scored 40 files against empty strings.
- **The LLM judge is not deterministic, even at temperature 0**, and
  over-drops files that contain one garbled loop line amid real
  conversation. The flow is therefore: judge → save verdicts → human
  reviews/edits `verdicts.json` → `--apply` executes the *saved* file and
  never re-judges.
- A drop deletes the sidecar, blanks the description it set (offline sink,
  `synced: false`; `AI: ` captions untouched), and journals an
  `arbiter-hallucination` skip so the next pass neither re-transcribes nor
  resurrects it.

Worst-80 verdict (2026-06-10): 78 of 80 confirmed/kept, 2 dropped as
hallucination-only — a clip where both engines looped on noise in
different words («We are twelve» vs «I'll stop by the boat»), and one
that is ~70 % exclamation loops. The two engines agreeing this rarely on
*what was wrong* is exactly why a single-engine pipeline needs the check.

GigaAM setup (PyPI 0.1.0 lacks v3 — install from GitHub; needs ≤ 3.12:
its `onnxruntime==1.23.*` pin has no Python 3.14 wheels):

```sh
python3.12 -m venv ~/.immy/venv-gigaam
~/.immy/venv-gigaam/bin/pip install 'gigaam @ git+https://github.com/salute-developers/GigaAM.git'
```
