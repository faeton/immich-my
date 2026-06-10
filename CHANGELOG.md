# Changelog

Notable changes and findings, newest first. Format is loosely
[Keep a Changelog](https://keepachangelog.com); this project ships
continuously, so entries are dated rather than versioned.

## 2026-06-11 — mass "Error loading image": paused thumbnail queue

### Findings

- **4,961 assets across 16 trips had no thumbnail/preview `asset_file`
  rows** (Svalbard 2,393; all five pacific trips; les-arcs; scotland;
  both norways; several 2024 trips) — grid tiles and the full-screen
  view both showed "Error loading image".
- Root cause was a chain: these assets were registered while their
  originals were still offline, so promote fell back to queueing
  `regenerate-thumbnail` jobs for Immich to run server-side — but the
  server's `thumbnailGeneration` queue was **paused**, with 8,821 jobs
  silently accumulating. The fallback never executed.
- The paused queue also broke **phone-app backups** (internal-storage
  uploads, `libraryId IS NULL`): Immich is the only thumbnail generator
  for those, so 9 iPhone HEICs sat with no derivatives at all.
- ~640 server assets (DJI LRF/LRV proxies, HYPERLAPSE stills) are
  offline+trashed — expected: their local sources were deliberately
  deleted; the library scan retired them server-side.

### Changed

- `immy repair-thumbs` across all trips: 4,920 thumbnails+previews
  regenerated locally and upserted (9,840 rows), 41 Svalbard assets had
  no local source. Full-library probe after: 10 broken of 7,462.
- Emptied the stale 8,821-job queue, queued regeneration for the 10
  remaining (9 phone HEICs + 1), **resumed `thumbnailGeneration`** —
  all verified loading. The queue must stay unpaused: promote's
  offline-asset fallback and every future phone backup depend on it.

## 2026-06-11 — Immich metadata refresh destroys descriptions

### Findings

- **Immich v2 rebuilds `asset_exif` from file tags on metadata
  extraction and overwrites every field not in `lockedProperties`.**
  Descriptions written via direct SQL carry no lock and no sidecar →
  a library scan after the 9-trip upload wiped 338 synced descriptions,
  replacing them with camera-embedded junk: '' (videos), 'default'
  (DJI), 'DCIM\…' paths or the file's own name (Insta360).
- **Video descriptions ignore XMP sidecars entirely** (v2.7.5 source:
  video path reads only `videoTags.Description || Comment` from the
  container). `PUT /api/assets` is self-defeating for videos —
  SidecarWrite unconditionally unlocks the field, then queues
  re-extraction: 197 API-pushed video descriptions were wiped again
  within minutes. Images survive (photo path prefers sidecar tags).
- The only durable, non-file-mutating mechanism for video descriptions
  is appending `'description'` to `asset_exif."lockedProperties"` via
  SQL in the same statement as the write — no job cycle unlocks it.
- Confirmed with Codex (immich source review) + Grok consults.

### Changed

- All immy description writes now also lock the field; camera
  boilerplate is treated as overwritable by every guard
  (`captions.is_camera_boilerplate`).
- Captions/transcript excerpts are mirrored into the local
  `basename.xmp` (`_mirror_description_to_xmp`) — image-path protection
  that travels with promote's rsync; skipped when the DB guard refused
  the write.
- New `tools/reconcile-descriptions.py` — server-vs-sink description
  diff; pushes diverged values (images via API, videos via SQL+lock);
  dry-run by default, ambiguous cases never touched.
- Offline-sink drain of the full library: 2 120 entries replayed
  (argentina's 8 missing CLIP `.npy` regenerated from staged previews,
  1 stale faces ref dropped); descriptions reconciled server-side.

## 2026-06-10 — full-library transcript run (overnight batch)

### Findings

- **Full sweep complete**: all 61 trips through `immy process --offline
  --with-transcripts --with-captions --captions-fill-missing`. 676 new
  sidecars (350 en / 300 ru / 26 uk), 3 122 gated skips (DJI denylist,
  Tesla dashcam no-audio, silent clips), zero errors. Library now at
  100 % coverage on every phase including captions; `immy bloat` scan
  found zero transcode candidates.
- **"Wood Wood"** is a new Whisper noise hallucination on water/splash
  audio (la-manga, blue-lagoon, peru) — emitted as runs of identical
  sub-second cues.
- **Blank segments defeated the loop collapse**: `format_srt` computed
  decode-loop runs over the raw segment list, where Whisper's interleaved
  blank segments break a run of identical cues — "Wood Wood" ×7 survived
  write-time scrub. 792 such cues landed across 67 fresh sidecars before
  the fix.
- **Stale transcript journal entries hide real gaps**: 88 entries pointed
  at sidecars deleted long ago. 57 were intentional (Insta360 twin dedup
  keeps only the `_00_` master), 5 belong to arbiter-dropped groups
  (stay dormant by design), 26 were genuine orphans — among them the 18
  antarctica clips journaled as Faroese/Nynorsk by a pre-constrained-
  detect run in April.

### Changed

- `fix(transcripts)`: loop detection now runs on the non-empty cue
  stream, matching the written SRT's view.
- Post-run scrub applied: 792 loop cues removed from 67 new sidecars.
- 26 orphaned journal entries cleared and re-transcribed under the
  constrained ru/en/uk language detect.

## 2026-06-10 — library-wide verification sweep, in-cue word loops

### Findings

- **Library sweep** (`tools/verify-transcripts.py`, 165 sidecars / 98
  unique audio tracks): 24 low-agreement files judged, 6 drops suggested,
  4 of them over-drops on human review (the judge's known failure mode —
  real conversation with one garbled line). 2 genuine silence
  hallucinations dropped («До встречи!» ×4 over 2 min; "Thank you." ×5 on
  30 s-aligned cues).
- **In-cue word loops**: a decoder loop packed into a *single* cue
  («селфи» ×55, «девочкой» ×54 inside one segment) is invisible to the
  cue-level collapse, which needs ≥ 6 identical consecutive cues. Found in
  3 of the 4 over-dropped files.
- **Twin sidecars from different vintages diverge**: one Peru clip's
  judged sidecar was truncated at 1:56 while its LRV twin held the full
  14-minute conversation — the verifier's "A is a subset of B" reason was
  literally correct. Twin groups deserve a consistency pass when judged.

### Changed

- `feat(hallucinations)`: `collapse_word_runs()` — runs of ≥ 5 identical
  words (case-/punctuation-insensitive) within a cue collapse to the
  first occurrence, at `format_srt` write time and in
  `tools/scrub-srt-hallucinations.py` for existing sidecars.
- 8 sidecars hand-cleaned across 4 twin groups (la-manga, NZ, Peru ×2):
  in-cue loops truncated, the truncated Peru twin replaced with its full
  LRV transcript, garbage-only cues removed.

## 2026-06-10 — ASR engine bench, worst-80 redo, dual-engine verification

### Findings

- **Engine bench** (28 files / 3.1 h mixed ru+en travel audio; RTFx = audio
  seconds per inference second, model load excluded):
  - *Qwen3-ASR-1.7B* (mlx-qwen3-asr, GPU): RTFx 11.6. Quality winner — zero
    boilerplate, near-zero loops, only challenger to hear «Привет, бандит!»,
    and the only engine that preserves each language in mixed ru/en scenes.
    Flaws: occasionally flips a Russian phrase to English; hallucinated
    Dutch once on a very noisy clip.
  - *Whisper large-v3* (pipeline default, GPU): RTFx 16.9. Even with the
    new anti-loop decode flags it still *generates* «DimaTorzok» /
    «Продолжение следует» boilerplate on 14 of 28 files (write-time scrub
    catches it) and silently translates Russian speech inside en-detected
    files.
  - *GigaAM-v3* (`v3_e2e_rnnt`): RTFx **39 on pure CPU**, cleanest Russian
    of all, zero hallucinations — but unusable English. Ideal second
    opinion, not a sole engine. Hard input limit of exactly 25.0 s
    (400 000 samples); needs the GitHub install (PyPI 0.1.0 lacks v3) and
    Python ≤ 3.12 (onnxruntime pin).
  - *Canary-1B-v2* (NeMo): eliminated — RTFx 6.2 CPU-only on Mac (MPS
    rejects float64) and catastrophic repetition loops («Наконец» ×97) on
    exactly the files under treatment.
- **Insta360 twins**: dual-lens (`_00_`/`_10_`) + LRV proxy files of one
  clip carry identical audio. 54 of the 80 worst files were twins — 6.7 of
  15.1 audio-hours would have been transcribed in duplicate. Transcribe
  once per group, fan out.
- **LLM-judge non-determinism**: the LM Studio arbiter (gemma-4-31b-it)
  gives different verdicts across runs even at temperature 0, and
  over-drops files that contain one garbled line amid real conversation.
  Apply steps must execute saved, human-reviewed verdicts — never re-judge.
- **Whisper-vs-Qwen verification verdict**: across the 59 unique worst-80
  audio tracks, median word-level agreement between Qwen and an independent
  engine was 0.56; after judging + human review only 2 of 80 files were
  hallucination-only. Qwen held up on the hardest corpus, but the pipeline
  default stays Whisper until a broader sample is verified.

### Changed

- The 80 worst hallucinated transcripts (8 trips, 2023-11 → 2025-11)
  re-transcribed with Qwen3-ASR-1.7B: sentence-level cues with word-aligned
  timing, scrubbed via the shared hallucination filter, descriptions
  backfilled (empty-guard), journal entries record
  `engine: mlx-qwen3-asr/Qwen3-ASR-1.7B`. Two clips dropped as
  hallucination-only after dual-engine review and journaled as
  `arbiter-hallucination` skips.
- New `tools/verify-transcripts.py` — dual-engine transcript verification
  with twin-group dedup, agreement scoring, LM Studio arbiter (dry-run by
  default, `--apply` executes reviewed verdicts).
- New `docs/TRANSCRIPTS.md` — transcript pipeline, engine bench, twin
  dedup, verification design and its operational gotchas.

## 2026-06-09 / -10 — transcript hallucination root causes

- `fix(transcripts)`: two silent-kill API drifts in mlx-whisper 0.4.3 —
  `detect_language` returning a bare dict (KeyError killed every transcript
  via `on_transcript_error="skip"`) and `VideoInfo.duration_s` rename.
  Adopted `word_timestamps` + `hallucination_silence_threshold=2.0`
  (validated by A/B on the worst loopers).
- `feat(hallucinations)`: boilerplate matching is now substring- and
  case-insensitive («продолжение следует», DimaTorzok in any form);
  decode-loop collapse (same cue ≥ 6× consecutively) at SRT write time.
- `fix(journal)`: caption skip paths (same-model sink, DB `AI: ` prefix,
  kept-prior) now converge the journal, so resumed runs stop re-walking
  finished work. New `tools/audit-journal.py` reports true per-trip
  coverage from disk files (journal keys are path-hashes — renames orphan
  them) with guarded stale-key pruning.
- Verified full-library state: 6 997 live assets at 100 % derivatives /
  CLIP / faces / captions; 1 115 stale journal keys pruned; 23 LRF ghost
  sink files quarantined (none existed server-side).

## 2026-06-08 — caption pipeline robustness

- `feat(captions)`: parallel `--caption-workers` pool; all DJI `.LRF`
  proxies dropped at ingest.
- `fix(captions)`: re-encode retry on LM Studio invalid-image 400 (corrupt
  staged JPEG — validate with djpeg, PIL is too lenient); per-trip
  heartbeat during the parallel pool; stale heartbeats ignored.
- Captioner pinned to gemma-4-31b-it via LM Studio (7-captioner bench:
  ~3 s warm, quality on par with cloud CLIs; qwopus ~14× slower, reserve
  for OCR-heavy trips).
