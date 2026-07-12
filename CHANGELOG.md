# Changelog

Notable changes and findings, newest first. Format is loosely
[Keep a Changelog](https://keepachangelog.com); this project ships
continuously, so entries are dated rather than versioned.

## 2026-07-12 — `srt geotag --relock` + `immy tags sync`

### Found

- **Drone/video clips silently missing location and gear tags in Immich.**
  Root-caused live on n5: `asset_exif.latitude/longitude` was populated for
  most DJI clips (map pin renders correctly) but `lockedProperties` was
  empty and `country`/`state`/`city` were NULL — an unlocked, ungeocoded
  state that falls through both existing safety nets (`srt geotag` skips
  any row with *a* coord already; `srt geocode` requires the lock token as
  proof-of-ownership before touching a row). Same video/XMP blind spot
  (`docs/TELEMETRY.md`) also silently drops notes-derived tags
  (`Gear/Camera/DJI FC8282`, trip/event tags) for every video asset — XMP
  sidecars work for photos only, and `promote --tag` only pushes whatever
  flat list was passed on that one invocation, not the trip's full notes
  `tags:`.

### Added

- **`immy srt geotag --relock`** (`srtgeo.py`, `cli.py`): repairs clips that
  already carry a DB coord but were never locked — locks + reverse-geocodes
  them, but only when the existing DB coord is within 2 km of the
  independently-computed `.SRT` fix (`_RELOCK_TOLERANCE_M`), so a location
  pinned by hand in the app is left alone (`skip-mismatch`).
- **`immy tags sync <trip> [--write]`** (new `tagsync.py`): pushes a trip's
  full notes `tags:` to every one of its assets via Immich's native Tag API
  — the durable, video-safe channel `trip-tags-from-notes`'s XMP write can't
  reach. Per-file gear-tag matching (`rules/trip_tags.tags_for_file`, `_propose`) is
  shared with the XMP rule so the two channels can't disagree.
- **Library-wide backfill run**: **865 GPS relocks + 25 fresh geotags** across
  the 30 trips with drone footage; **7,702 tag placements** across all 65
  notes-bearing trips (distinct tagged assets in `tag_asset` went 5,332 → 7,489
  — spot-checked via direct Postgres query, not just the CLI's own summary).

### Fixed

- **`ImmichClient.upsert_tags` silently dropped every hierarchical tag
  attachment.** The first `tags sync --write` backfill reported "tagged 7,702
  asset(s)" with exit 0 and zero errors — but `tag_asset` row counts hadn't
  moved. Live `GET /api/tags` showed why: for a hierarchical tag (`Gear/
  Camera/DJI FC8282`), Immich's response `name` field is just the leaf
  segment (`"DJI FC8282"`); the full path lives in `value`. `upsert_tags`
  keyed its `{name: id}` return by `name`, so every caller's lookup by the
  full path missed, and the follow-up `tag_assets` call for that tag never
  fired — while `upsert_tags` itself legitimately succeeded (the tag rows
  really were created), so nothing surfaced as an error anywhere. Fixed to
  key by `value` (falls back to `name` if absent). `tag_sync_folder` also no
  longer marks a row `"tagged"` until *after* confirming the API actually
  returned an id for every tag it needed (`"tag-failed"` otherwise) — this
  exact gap (optimistic status before the write) was independently flagged
  by a Codex pre-commit review before the root cause was even isolated. A
  follow-up Codex pass on the fix itself then caught a second, narrower gap
  in the same spirit: `tag_assets()`'s own per-asset response was still
  unchecked, so a genuine attach failure (as opposed to `error="duplicate"`,
  which is expected/idempotent) would've still read as `"tagged"`. Closed by
  inspecting `tag_assets()`'s result too. Re-ran the full backfill after each
  fix; verified via Postgres, not just the CLI's exit code — final state:
  7,489 distinct assets carrying a native tag, `tag-failed=0` across all 65
  trips.

### Also

- **`immy tags camera <trip> [--write]`**: backfills the Details panel's
  blank "Camera" row for DJI clips (`asset_exif.make`/`model`, empty because
  the MP4 container carries neither) from the trip's notes `Gear/Camera/
  <make> <model>` tag — same source `tags sync` resolves, split on the first
  space. Verified live with a `srt verify-channel`-style probe first: unlike
  GPS, Immich's metadata refresh doesn't clobber an *unlocked* make/model
  write either (it only ever sets these fields from a fresh file read,
  never nulls them when the file has none) — locked anyway as a safety net.
  Never overwrites an asset that already has make or model set, so it can't
  clobber Immich's own extraction for any non-DJI camera.
- **Legacy tag cleanup**: found 78 malformed flat tags across the library
  with a literal `|` in their name (e.g. `Gear|Camera|DJI FC8282`) predating
  this session — a past bug (unrelated to the one above) that passed
  hierarchical-looking names through the wrong separator. Deleted the 59
  that were fully redundant with the correct `/`-hierarchical tag (verified
  every attached asset already carried the correct equivalent first); left
  19 alone where the pipe tag's assets weren't fully covered (renamed trips
  whose old event name has no current equivalent, or gear not reflected in
  current notes) rather than guess.

Reviewed twice more end-to-end (Codex resumed thread + a fresh Grok pass)
against the merged commit — both independently confirmed no further
correctness issues; findings were operational hardening (non-zero exit on
partial failure, per-trip error isolation for multi-trip runs) and applied.

## 2026-06-23 — `immy match` + snapshot v2

### Added

- **`immy match <inbound>`** (`match.py`, `cli.py`): read-only, fully offline
  triage of a folder about to be imported. Reports per top-level **subfolder**
  and per self-clustered **event**: which files are already in Immich (dedup
  via the `find-duplicates` classifier), which belong to an existing trip
  (`matched`/`extends`), and which are `new`. `⚠ spans multiple trips` flags a
  folder that straddles trips. `--thorough` hashes every file (catches
  renames); `--max-km`/`--max-gap-hours` tune placement. `--fast`/`--no-verify`
  (`HashMode.FAST`) trusts a name+size hit and skips SHA1 — turns a ~2 TB
  already-promoted tree from ~50 min (SHA1-bound) into ~2 min (exiftool-bound).
- **Existing trips reconstructed offline** (`build_existing_trips`): every
  snapshot album becomes a trip keyed by name, with **IQR-fenced date bounds**
  + **median centroid / 90th-pct radius** so one misdated/mislocated asset
  can't blow a trip's window to 9 years and date-match everything; un-albumed
  assets are re-clustered with the `immy cluster` sweep.
- **GPS-less fallback**: drone/video clips with no EXIF GPS are placed
  **date-only** (labelled lower-confidence); tally points at `immy srt geotag`.
- **Snapshot schema v2** (`snapshot.py`): `assets` gains `lat/lon/city/country`;
  new `albums` + `album_assets` tables. `fetch_albums` keeps **every** album
  (real albums are trip-named but carry no `immy-cluster` marker — marked-only
  would drop them all). `require_schema` rejects v1; `find-duplicates` stays
  v1-compatible. Docs: [docs/MATCH.md](docs/MATCH.md). Reviewed by Codex + Grok.

## 2026-06-19 — SRT telemetry pipeline (`immy srt`)

### Added

- **Full DJI `.SRT` track parser** (`srt.py`): `parse_track()` →
  per-frame `SrtFrame` (t, lat/lon, `rel_alt`/`abs_alt`, iso/shutter/fnum/
  ev/focal_len). Handles the combined `[rel_alt: .. abs_alt: ..]` bracket,
  legacy `[altitude:]`, and `GPS(...)`. `first_valid_fix()` skips `(0,0)`
  pre-lock frames (the takeoff point). `parse()` keeps its first-fix API
  (streams cues, early-stops) so `dates`/`backfill_dates`/`rules.dji_srt`
  are unchanged — and now gain the 0,0-skip for free.
- **Track sidecars** (`track.py`): `<stem>.gpx` (GPX 1.1, round-trips
  through `rules.geotag_from_gpx`) + `<stem>.track.json` (per-frame
  telemetry + summary). Placed via new `WritablePaths.gpx_path` /
  `track_json_path` — on the NAS they mirror under `sidecars_root`, never
  beside the `:ro` originals.
- **`immy srt` CLI group**: `track` (emit sidecars), `geotag` (durable DB
  GPS from takeoff fix, dry-run by default, `--write` to apply), and
  `verify-channel` (the empirical probe below).
- **Durable video geotag** (`srtgeo.py`): `UPDATE asset_exif` lat/lon +
  append `latitude`/`longitude` to `lockedProperties` — mirrors how
  descriptions are made refresh-proof. Idempotent (skips assets that
  already carry DB coords); triggers Immich's reverse-geocode.
- **Caption context** (`captions.caption(context=…)`): drone clips now feed
  `~{rel_alt} m above ground` + place (notes `location.name`, else cached
  reverse-geocode) into the VLM prompt. Threaded through both caption paths
  in `process.py`; non-drone media stays byte-identical.
- **Reverse-geocode** (`geocode.py` + `immy srt geocode`): replicates Immich
  v2.7.5 `MapRepository.reverseGeocode` against the *same* Postgres —
  `geodata_places` nearest within 25 km (`earthdistance`), `naturalearth_countries`
  polygon fallback — and maps `countryCode`→name via the vendored
  i18n-iso-countries 7.6.0 'en' dataset. So drone clips get country/state/city
  **identical** to the rest of the library, fully offline. `srt geotag` writes
  place inline; `srt geocode [--prefix]` backfills from DB coords (no files).
- 27 new tests (`test_srt`, `test_track`, `test_srtgeo`, geocode + caption-context);
  multi-frame DJI fixture. 471 pass.

### Findings

- **verify-channel result (run live on n5, DJI_0073.MP4)**: for VIDEO
  assets a metadata refresh **clobbers an unlocked `asset_exif` GPS to
  NULL** (Immich re-reads container tags, finds none — XMP sidecars are
  images-only). An `UPDATE` **+ `lockedProperties` lock with tokens
  `latitude`/`longitude` survives**. So the XMP-sidecar geotag from the old
  `dji-gps-from-srt` audit rule never reaches the DB for drone videos —
  `srt geotag`'s lock is the only durable channel. Probe is non-destructive
  (restores the asset).
- **First live run (2024-02-peru-bolivia)**: 230 NULL-GPS drone clips tagged
  from their SRT takeoff fix; GPS landed + locked, **survives refresh
  (gps_lost=0)** → map pins now work.
- **Locked coords are never auto reverse-geocoded — confirmed in source.**
  Immich v2.7.5 `metadata.service.ts` only geocodes coords read *fresh from
  the file* (`if (hasGeo(fileExif))`), never the DB value; our drone videos
  have no file GPS and read-only originals, so no Immich path (refresh *or*
  the asset-update API) will ever geocode them. The `PUT /api/assets/{id}`
  route is also destructive on `:ro` originals — it queues a `SidecarWrite`
  that can't land and the live test *wiped* a good geotag (restored).
- **Resolved by self-geocode from Immich's own geodata.** Cross-checked the
  port against 1,500 already-geocoded assets: **country/state/city 100 % match**.
  Backfilled the 230 peru-bolivia clips: country 100 % (Bolivia 163 / Peru 67),
  city 97 (rest are >25 km from any geodata place → country-only, same cutoff
  as Immich). GPS stays locked + intact.

## 2026-06-19 — backup automation: nightly n5→vv mirror (Phase 3)

### Added

- **`immy/deploy/n5/backup/`** — `nightly-mirror.sh` + `mirror.env.example` +
  `README.md`. Self-contained nightly job implementing Phase 3 of the primary
  swap: perm self-heal (originals → `faeton:faeton 755/644`) → `pg_dumpall`
  (verified, with a `RESTORE-RECIPE.txt`) → atomic ZFS-snapshot rsync of
  `originals/` + `media/{library,profile,upload}/` to vv → push dump to `vv:db/`.
  `flock` (no overlap), Healthchecks dead-man's-switch (`/start` + success +
  `/fail` with log tail), `--max-delete=200` guard, `DRY_RUN` toggle.

### Findings

- **Native TrueNAS tools can't own this**: ZFS Replication needs a ZFS receiver
  and vv is a Synology (btrfs); SCALE Rsync Tasks have no pre/post hooks (can't
  sequence dump/perm/ping around the copy); `pg_dump` has no native task. So the
  orchestration is a script, run by a **native Cron Job** (id 3, `0 5 * * *`,
  user `faeton`); existing Periodic Snapshot Tasks stay independent.
- **Run as faeton + `sudo`, not root**: faeton's vv ssh key already works, so we
  avoid setting up root→vv auth; `sudo` covers zfs/docker/chown. Pushing as
  faeton@vv (no `--numeric-ids`) lands files owned by vv's own faeton — the perm
  story vv wants, for free.
- **`media/library` is `0777`** (Immich chmods everything world-readable), so a
  faeton-run mirror reads it fine — the perm self-heal only needs `originals/`.
- **Config-file vs env footgun**: sourcing `mirror.env` (which carries
  `DRY_RUN=0`) clobbered a `DRY_RUN=1` passed on the command line, so the first
  "dry-run" did a full live mirror. Fixed: the CLI/env `DRY_RUN` is captured
  before sourcing and wins. (Pipeline thereby validated live end-to-end.)

### Deployed

- Installed to `n5:/mnt/tank/scripts/immich-mirror/` (faeton-owned; lock + logs
  live beside the script). Cron job id 3 materialized in `/etc/cron.d/middlewared`.
  **TODO (user):** paste the Healthchecks check URL into `mirror.env` (`HC_URL=`)
  — until then a *missed* run won't alert.

## 2026-06-19 — immy runs on the N5; read-only-originals refactor

### Findings

- **Immich's ML container has no published port** (`immich_machine_learning:3003`,
  internal-only on `ix-immich_default`) — so to use Immich's own CLIP, immy must
  run *inside* that docker network, not over the tailnet. That's the whole reason
  for packaging immy as a container on the NAS.
- **`host.docker.internal` (host-gateway) is unreliable here**: reaching a
  published port from a container on `ix-immich_default` hits a hairpin-NAT bug —
  TCP connects but HTTP responses get reset. Fix: address every backend by
  container name on the shared network (attach Ollama + the qwen-asr shim to it).
- **`process` wrote all state under the trip folder** (`.audit/` journal, marker,
  heartbeat, staged derivatives; `.srt`/`.xmp` next to media) — fatal on the NAS
  where originals are a read-only mount of the live external library. Even a
  captions-only run failed, because the caption `.xmp` mirror wrote beside originals.
- **Scope decision** (codex+grok review): on the NAS immy does **captions +
  transcripts only**; Immich keeps doing its own CLIP/faces/thumbnails (ML on by
  default). Cross-machine dedup (Mac ⇄ NAS) already works via the DB AI-prefix
  check and existing-`.srt` detection — no new queue needed yet.

### Changed

- **Phase 6 — packaging** (`immy/Dockerfile.immy`, `immy/deploy/n5/`): standalone
  compose that joins `ix-immich_default`, lean image (`--no-deps`, no onnx/mlx),
  one-shot `docker compose run --rm`. Not folded into the TrueNAS-managed stack.
- **Phase 6.1 — writable-state refactor**: new `state_root` / `sidecars_root`
  config (env `IMMY_STATE_ROOT` / `IMMY_SIDECARS_ROOT`) + `immy/paths.py`
  resolver threaded through every write site. Unset = Mac path **byte-identical**;
  set = state → `state_root`, sidecars → `sidecars_root`, originals stay read-only.
- `run-batch.sh` defaults to `--with-captions --with-transcripts --no-clip --no-faces`.
- 444 tests (+9: defaults-match invariant, NAS-mode redirect, a `chmod 0555`
  read-only-trip proof). Verified on the N5: build, four-backend reachability,
  dry-run with `originalPath` anchored correctly.
- The heavy opportunistic-worker queue (claim ledger, `immy worker pull`, GPU
  scheduler) is **deferred** until the consolidation import waves need it.

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
