# Testing

Per-phase acceptance tests. A phase is "done" only when its tests pass. Each
test is a **golden path** (happy case) plus at least one **failure mode** that
exercises the thing the phase was supposed to protect against.

Conventions:
- `vv` = the DS923+ over Tailscale.
- `mac` = the MacBook.
- Commands run on the NAS use the full docker path (`/usr/local/bin/docker`)
  because DSM's default shell `$PATH` doesn't include it.

## Phase 0 — Base stack

| # | Test | Pass criteria |
|---|---|---|
| 0.1 | Containers healthy after host reboot | `docker compose ps` from `/volume1/faeton-immi/docker/` shows all four containers `healthy` after a full DSM reboot, without manual intervention. |
| 0.2 | Web UI reachable over Tailscale | `https://vv.<tailnet>:2283/` (or whatever tailnet name we pick) loads the Immich login in < 3 s from the Mac and iPhone. |
| 0.3 | Admin account sign-in works | Can log in with the account created at first boot; no banner warnings in Admin → Server Stats. |
| 0.4 | iOS app round-trip | From the Immich iOS app, point at the Tailscale URL, log in, take one photo, trigger backup. Photo appears in web UI timeline within 60 s. |
| 0.5 | Storage template applied | After 0.4, the file lives at `/volume1/faeton-immi/library/library/<user>/2026/2026-04-16/HHMMSS-<origname>.<ext>`. |
| 0.6 | External library visible | Admin → Libraries shows `/mnt/external/originals` as an external library. Scan completes with 0 assets (empty tree) and no errors. |
| 0.7 | External library picks up a dropped file | Copy one `.jpg` into `/volume1/faeton-immi/originals/2026/2026-04-16/test.jpg`, re-run the scan, the asset appears read-only in the timeline. |
| 0.8 | Postgres dump + restore | Run the `pg_dumpall` from DEPLOY.md, verify gzip opens and contains `CREATE DATABASE immich`. (Full restore rehearsal deferred until we have a second test NAS or a VM.) |
| 0.9 | Graceful degradation: Mac offline | With the Mac asleep, browsing + search-by-filename still work. (CLIP/face jobs may queue — expected. Nothing should *break*.) |
| 0.10 | Port 2283 not exposed to WAN | From an off-tailnet device with only the NAS's public IP, `curl` to port 2283 fails/times out. (Sanity: we never opened the router.) |

**Regressions to watch for:**
- `immich_postgres` logs growing because `DB_STORAGE_TYPE: HDD` is uncommented
  accidentally (we're on SSD-cached btrfs — leave it commented).
- `model-cache` bind mount permissions: a failed pull leaves the dir root-owned
  and ML container sits in a loop. Symptom: ML jobs stuck in "active" forever.

## Phase Y — direct-to-Immich-DB

Tests assert on the SQL surface (mocked psycopg) plus one end-to-end smoke
against the real NAS PG whenever a Y slice lands.

| # | Test | Pass criteria |
|---|---|---|
| Y.1 | `immy process` inserts idempotent rows | ✅ Unit: `sha1("path:"+abs)` is 20 bytes and matches the handwritten spec; `build_rows` populates owner/library/type/checksum/dates from fixtures; `insert_asset` emits two `execute` calls (asset then exif) with the right params; on checksum conflict the exif INSERT is suppressed. CLI: `--dry-run` touches no cursor and writes no marker; real run commits, drops `.audit/y_processed.yml`, and re-run reports "0 new, 1 already present". Promote: with marker present, `fake.scan_library` is never called. Smoke-tested against the DS923+ PG with the DJI fixture — row + exif + GPS all landed as expected. |
| Y.2 | Thumbnail + preview derivatives | pending |
| Y.3 | CLIP `smart_search` row | pending |
| Y.4 | Faces | pending |
| Y.5 | Video proxy | pending |
| Y.6 | Accelerator uninstalled | pending |

## Phase 1 — Mac as burst ML node

Operating constraint: Mac is mobile, often on 5–10 Mbps uplink, sometimes
off-tailnet entirely. All tests assume Mac-unreachable is a normal state,
not a failure.

| # | Test | Pass criteria |
|---|---|---|
| 1.1 | `immich-ml-metal` reachable from NAS | From `vv`: `curl -sf http://<mac-tailscale>:3003/ping` returns 200 with the Mac awake and on tailnet. |
| 1.2 | Immich prefers Mac when available | With both URLs configured, a 100-photo face backfill shows ML jobs hitting the Mac (check process list / container logs). |
| 1.3 | Fallback when Mac sleeps / off-tailnet | Put the Mac to sleep (or disable Tailscale on it) mid-backfill. Jobs drain on Syno fallback within the configured timeout (2–3 s per attempt). No job stuck in "active" > 10 min. |
| 1.4 | 50k backfill budget (stable link) | With Mac awake on mains + reliable Tailscale, full face backfill on 50k assets completes in ≈ 1 h wall clock. (Fails loudly if we regressed to CPU path.) |
| 1.5 | 50k backfill budget (Mac offline) | Same backfill with Mac never reachable: completes on Syno CPU alone, finishes, no wedged jobs. Wall clock is hours, not minutes — that's fine. |
| 1.6 | Captive-portal / flaky link survival | Simulate with `pfctl`-throttled tailnet + periodic drops during a backfill. Progress is monotonic; no duplicated work on reconnect (idempotent on `(checksum, worker, version)`). |
| 1.7 | Mac reboot / lid-close is a no-op | Reboot the Mac or close the lid mid-queue; on return, queue resumes where it left off. |

## Phase 1b — Mount adapter framework

| # | Test | Pass criteria |
|---|---|---|
| 1b.1 | SMB mount health check | Wrapper reports `healthy=true` for a live SMB share, `healthy=false` after the provider goes down (simulate with `ifconfig` down on the source). |
| 1b.2 | Unplug-mid-scan doesn't wedge | Start an external-library scan against a USB drive, yank the drive. Scan returns an error within 60 s; Immich server process stays up; UI stays responsive. |
| 1b.3 | Thumbs keep rendering offline | After 1b.2, timeline thumbs for offline assets render from tier-0 derivatives. Only "open original" fails, with the friendly message. |
| 1b.4 | `rclone` VFS cache capped | Fill-up simulation: cache dir on NVMe never exceeds the configured cap; oldest blocks evicted first. |
| 1b.5 | Catalog-only toggle | For a source marked catalog-only, ingest reads header + preview but never pulls full bytes (verify via `rclone mount --vfs-read-chunk-size` counters or SMB byte counters). |

## Phase 2a — `immy` metadata forensics

### Test pyramid

| Level | What it tests | How |
|---|---|---|
| **Unit** | Each rule's `match()` + `fix()` in isolation | Dict inputs, no file IO, golden outputs. Fast (ms). |
| **Exif roundtrip** | pyexiftool reads/writes are correct | Write EXIF → re-read → assert. Catches library drift. |
| **Rule on fixture** | One rule applied to a realistic mini-folder | Snapshot XMP sidecars + `state.yml` → compare to goldens. |
| **End-to-end, mocked** | Full `audit → promote` against a fake Immich | `respx` mocks the REST API. Runs in CI, zero network. |
| **End-to-end, staging** | Full run against real `nas-media` Immich, dedicated test external library | Nightly / on tag. Catches environment drift. |
| **Idempotency** | Re-running is a no-op | Run audit twice; second run diffs clean. |
| **State persistence** | Decisions survive re-runs | Answer once, re-run, ensure no prompt fires. |

### Fixtures (committed to `immy/tests/fixtures/`)

Shipped:
- `dji-srt-pair/` — `DJI_0001.JPG` + `DJI_0001.SRT` carrying Casela GPS + wall-clock date. Drives `dji-gps-from-srt`, `dji-date-from-srt`.
- `insta360-pair/` — `VID_20240101_120000_00_001.insv` + `LRV_20240101_120000_01_001.lrv`, timestamp+serial match. Drives `insta360-pair-by-ts-serial` + `date-from-filename-vid-img`.
- `trip-anchor-simple/` — two GPS-less JPGs + `TRIP.md` with explicit `location.coords`. Drives `trip-gps-anchor` and the `sibling SRT beats anchor` precedence test.
- `clock-drift-simple/` — four JPGs with EXIF `DateTimeOriginal`, three clustered on 2026-04-01 and one four days later. Drives `clock-drift` (MEDIUM) + the interactive y/n prompter + `--yes-medium` flag.
- `tag-suggest-missing/` — two Nikon-EXIF `DSC_*.JPG` files + a `TRIP.md` whose `tags:` list only has `Events/CustomEventLabel`. Drives `tag-suggest-missing` (MEDIUM) + the `write_notes` apply action + cascade through `trip-tags-from-notes` to XMP.

Pending (for later iterations):
- `timezone-naive/` — `DateTimeOriginal` present, offset missing (already exercised indirectly via 2a.1+; may need its own fixture for mixed-timezone trips).
- `export-date-trap/` — `ModifyDate` ≫ `DateTimeOriginal` (2a.5).
- `multi-camera-clean/` — all cameras aligned, expected output is zero prompts.
- `golden/` — byte-exact expected XMP sidecar outputs + `state.yml` per fixture (defer until a rule changes often enough for goldens to earn their keep).

Files are tiny — a real 1×1 JPEG + a hand-written SRT is plenty; empty `.insv`/`.lrv` work for pairing tests because the rule only needs exiftool not to choke on them.

### Per-iteration acceptance

| # | Iteration | Pass criteria | Status |
|---|---|---|---|
| 2a.0 | Skeleton | `immy --help` works; `immy audit ./fixtures/dji-srt-pair` prints table, exits 0; one passing pytest. | ✅ |
| 2a.1 | Four HIGH rules + XMP + state | `immy audit --write ./fixtures/dji-srt-pair` creates `DJI_0001.xmp` with GPS+date from SRT. Second run: zero writes (state.yml idempotency, `audit.jsonl` doesn't grow). Insta360 pair recorded. | ✅ |
| 2a.1+ | Folder-notes-driven rules | `trip-gps-anchor` applies coords from front-matter; interactive prompt writes user input back to notes; `trip-tags-from-notes` lands `HierarchicalSubject`+`Subject`; `trip-timezone` suffixes `XMP:DateTimeOriginal` with `±HH:MM`. Sibling-SRT GPS beats trip anchor per per-field dedup. Two-pass apply converges at fixed point. | ✅ |
| 2a.2 | Clock drift + MEDIUM prompter | `clock-drift-simple` fixture (3+1 outlier) surfaces one MEDIUM finding with source+delta reason. `--yes-medium` applies XMP `DateTimeOriginal` = folder median; re-audit is clean (outlier now within 24 h of new median). Interactive y applies, n skips without writing, `--auto` alone reports without applying. Under `MIN_SAMPLES=3`, small folders (trip-anchor-simple) never see the rule fire. | ✅ |
| 2a.3 | Tagging | `tag-suggest-missing` fixture: MEDIUM prompt surfaces exactly the categories the user's `tags:` list is missing (never duplicates an already-populated category). `--yes-medium` merges into notes and cascades to XMP via `trip-tags-from-notes`. `tag_suggestions: off` (YAML bool *or* string) opts out. Re-audit is clean; no re-prompt. Immich-round-trip of the hierarchy lands with 2a.4. | ✅ |
| 2a.4 | Promote + scan trigger | `immy promote ./fixtures/dji-srt-pair` rsyncs to a local `originals-test/` tree (staging before wiring a real remote NAS target). Mocked `POST /api/libraries/:id/scan` fires exactly once per run. `--dry-run` performs zero writes and zero API calls. Mocked `POST /api/stacks` fires once per `.insv`↔`.lrv` pair with the `.lrv` asset ID as primary. Guard rail: refuses with exit 1 if HIGH findings are still pending; `--force` overrides. Aliases `push` and `pub` behave identically. `.audit/` excluded from destination. | ✅ |
| 2a.5 | Real-trip coverage | Two actual Incoming trips (e.g. `La Manga`, `Mau-Lions-1`) audit with <10 % of files flagged LOW-confidence; no rule throws. Already demo'd on `Mau-Lions-1`: 197 HIGH findings apply cleanly, 0 LOW pending. All advisory rules landed: `makernote-present` (flags vendor MakerNote blocks, emits exiftool strip command, no writes) and `geocode-place` (`location.name` → Nominatim → `location.coords` in notes, cached at `~/.immy/places.yml`, silent offline). | ✅ |
| 2a.6 | Watcher | Drop a fresh fixture folder into `~/Documents/Incoming/` — within one debounce cycle, `launchd`-run `immy` audits non-interactively; if all rules are HIGH it promotes, otherwise writes `NEEDS_REVIEW.md` at the folder root. | deferred (build if/when the backlog warrants hands-off ingest) |
| 2a.7 | Web answers | For every rule with `confidence: low`, there is either a terminal-readable prompt or a web form at `/audit/...` that resolves it. | pending |

Current smoke suite: 119 tests total (38 smoke + 13 promote + 12 bloat + 12 bloat-transcode + 6 notes-body + 6 geotag + 3 makernote + 6 geocode + 23 process), 119/119 green under `uv run pytest`. Bloat-transcode coverage: `target_bitrate` math for 1080p30 and 4K60, candidate built with savings math for fat H.264, candidate dropped when savings under 20 %, preserve allowlist honoured in CLI module (DJI_ prefix, Insta360 MP4 export), `group_by_folder` preserves encounter order, `optimized_path` stem handling, `apply_one` renames original + writes sha256+size receipt, byte/bitrate formatters. Promote album coverage: creates album with description from notes body, updates existing album's description without re-creating, skips album entirely when no Immich creds. Notes-body coverage: strips front-matter + `# Title` + scaffold-hint italic paragraph, keeps real prose after the hint, handles no-front-matter files, preserves paragraph breaks, empty-body returns "". Makernote coverage: flags files with any `MakerNotes:*` key present, silent on files without, one finding per file. Geocode coverage: fires when `location.name` set + `coords` missing, skips when coords already present, skips with no name, silent on Nominatim failure, reuses local cache without re-querying, persists new results to cache after network hit. Geotag coverage: matches nearby GPX point with notes `timezone:`, skips points outside 5-min threshold, skips naive dates with no tz signal at all, honours `EXIF:OffsetTimeOriginal`, leaves already-geotagged files alone, parses GPX 1.0 and 1.1 namespaces. Bloat coverage: flags fat H.264 and obscene HEVC; skips sane H.264, camera-native prefixes (DJI_, GX, VID_YYYYMMDD, DSC_), `.insv` extension, Insta360-made MP4 exports, ProRes `.mov`, `/raw|source|edit|project/` folders, non-video files; computes bitrate from filesize÷duration when AvgBitrate is missing. Audit coverage: help, empty folder, read-only audit, write+idempotency, SRT-vs-anchor precedence, insta360 pair recorded, notes scaffold, tags written to XMP, interactive coords prompt with piped stdin, interactive timezone prompt writes zone to notes + cascades to XMP, tz prompt rejects unknown zone, tz prompt skipped when already set, timezone suffix, timezone no-op when no date, notes-not-overwritten, clock-drift flag-only in read mode, `--yes-medium` median write + re-audit clean, interactive y/n for MEDIUM, `--auto` without `--yes-medium` reports but skips, `MIN_SAMPLES=3` gate, tag-suggest flags missing categories, `write_notes` merge + XMP cascade, `tag_suggestions: off` opt-out, tag-suggest idempotent after accept, export-date-trap flags files missing DTO, export-date-trap silent when DTO present, clock-drift-by-camera flags offset group, --yes-medium applies per-camera delta, batch prompt is one y/n for whole camera, below-5-min-noise skipped, above-14-day sanity cap skipped, single-camera folder still uses folder-median clock-drift. Promote coverage: `--dry-run` skips writes and API; rsync + scan trigger; `push` and `pub` aliases; pending-HIGH guard rail; `--force` override; Insta360 stack call (primary is `.lrv`); rsync-only when no Immich creds; missing-config error; `.audit/` excluded; idempotent re-run.

### Failure modes to cover

- **exiftool version drift** — fixture test that fails loudly when exiftool's output schema changes shape (so we catch it in CI, not at 3 am).
- **Partial write** — power cut mid-XMP: the next audit recovers, no orphan lock files, no corrupted sidecars.
- **Network flap during promote** — rsync or the API call fails → state rolls back, nothing half-promoted, re-run is safe.
- **Rule contradiction** — two rules match the same field with different fixes: fail the audit with a clear error naming both rules. No silent "last-rule-wins" behaviour.
- **`state.yml` corruption** — malformed file → `immy audit` refuses to run, points at a backup. Never silently re-asks questions.

### What **not** to test

- Full 760 GB library — that's theatre, not testing. Use representative fixtures + real pilot trips.
- Performance on huge trees — defer until a real audit feels slow in practice.
- Every `exiftool` edge case — we trust the tool; we test our wrapper.
- The Immich API itself — we trust upstream; we test our client.

## Phase 2 — Ingest funnel

| # | Test | Pass criteria |
|---|---|---|
| 2.1 | Inbox → originals move is atomic | Drop a 5 GB MP4 into `/library/inbox/…`. File appears in `/library/originals/…` only after normalisation finishes; no half-written file ever lives in `originals/`. |
| 2.2 | EXIF read is header-only | Drop a 20 GB ProRes clip. exiftool byte counter reports < 1 MB read; Immich asset metadata is complete within 10 s. |
| 2.3 | `osxphotos --update` is idempotent | Run twice back-to-back with no new Photos.app changes; second run creates zero new assets, zero DB writes. |
| 2.4 | People names survive the pull | A named person in Apple Photos → matching `faces` entry in Immich (or at least a consistent XMP sidecar ready for Phase 5). |
| 2.5 | Duplicate path, same file, noop | Re-rsync the same SD card into the same per-camera folder; no duplicate assets created. |

## Phase 2b — Lazy preview extractor

| # | Test | Pass criteria |
|---|---|---|
| 2b.1 | RAW thumb is the embedded JPEG | CR3/ARW/NEF/DNG: generated thumb's SHA matches the embedded preview extracted by exiftool, proving we didn't decode the RAW. |
| 2b.2 | HEIC thumb is the embedded one | Same — no decode-and-re-encode round-trip. |
| 2b.3 | MP4 poster from moov only | A 20 GB ProRes ingest completes in < 5 s to "has-thumbnail" state; background proxy job follows. |
| 2b.4 | `.insv` pairs with `.lrv` | Dropping both files into inbox produces one Immich asset with the `.lrv` used as preview, original `.insv` linked. |
| 2b.5 | DJI `.SRT` → XMP GPS | After ingest, the asset has GPS + altitude set from the SRT; visible on the Immich map. |

## Phase 2c — Bloat detector + batch re-encode

| # | Test | Pass criteria |
|---|---|---|
| 2c.1 | Allowlist respected | ✅ covered by `test_bloat.py` + `test_bloat_transcode.py`. All Insta360 `.mp4` exports (5.7K, 7.7K, equirect 2:1) never flagged — even at "obscene" bits/pixel. Ditto `DJI_`, `GX`, `GH`, `GOPR`, `MVI_`, `.dng`, `.braw`, `prores`, `dnxhd`, anything in `*raw*`/`*source*`/`*edit*`/`*project*` folders. |
| 2c.2 | Never auto-transcodes | ✅ `immy bloat transcode` prompts per group with `[y/N]` default `n`; `--yes` is the only path to skip prompts and still groups output for progress. Without `--apply`, originals are never replaced. |
| 2c.3 | Grouped-by-folder UI | ✅ `immy bloat list` prints one header per parent folder with file count, aggregate size, and estimated savings. Matches feedback_transcode_confirm. |
| 2c.4 | Sample before commit | Deferred. Non-destructive default (`.optimized.<ext>` sidecar) serves the same purpose — user can play the sidecar file in QuickTime before `--apply`. A true 10-second sample mode can land with the gap-fill web UI. |
| 2c.5 | Idempotent replace | ✅ `transcode_one` writes to `.part`, verifies duration + stream count via ffprobe, renames to `.optimized.<ext>`. `apply_one` atomic-renames original to `<name>.original` and writes `.transcode.json` with `pre_sha256`, `pre_size`, `post_size`, codec, bitrate. |
| 2c.6 | Asset identity preserved | Manual test on first real `--apply` run — needs `POST /api/assets/jobs` refresh against the Immich asset. Not a unit test (requires a live library). |
| 2c.7 | `preserve=true` XMP honoured | Deferred — no rule in immy reads `preserve=true` from XMP yet. Current escape valves: preserve allowlist (extensions, prefixes, codecs, Insta360, folder segments). Add when a real asset needs it. |

## Phase 3 — Proxy-first AI enrichment

| # | Test | Pass criteria |
|---|---|---|
| 3.1 | Whisper writes `.srt` sidecar | A 2-minute test video produces a sidecar SRT next to the proxy; asset description contains first line(s). |
| 3.2 | CLIP / transcript search hits | Searching for a unique phrase spoken in 3.1's video returns that asset. |
| 3.3 | Caption appended, not replaced | Captioner output is prefixed `AI:` so hand-written descriptions aren't clobbered. |
| 3.4 | Workers never open originals | During a full enrichment pass, audit byte reads of `/library/originals/`: zero bytes from the enrichment workers. |
| 3.5 | Resume after crash | Kill the Whisper worker mid-queue; restart; it picks up the next un-done `(checksum, worker, version)` row — no duplicated work, no skipped asset. |

## Phase 4 — Event clustering

| # | Test | Pass criteria |
|---|---|---|
| 4.1 | Trip becomes one album | A known travel week from last year forms exactly one album (not fragmented into multiple), named like `2025-07 Porto`. |
| 4.2 | No album for singletons | A one-off photo from a random day produces no album. |
| 4.3 | Re-run is stable | Running the job twice produces the same album set; no `2025-07 Porto (2)` duplicates. |
| 4.4 | Nominatim failures are soft | With Nominatim down, albums still form but get placeholder names; re-run later fills the real name. |

## Phase 5 — Metadata gap-fill UI

| # | Test | Pass criteria |
|---|---|---|
| 5.1 | Missing-GPS grouping | A 200-photo trip with no GPS shows as one group with a suggested location from nearest neighbour. |
| 5.2 | Apply-to-group writes XMP + API | After "apply", EXIF on disk and Immich API both reflect the new GPS; moving the file preserves the tag. |
| 5.3 | Nothing gets tagged until click | Until the user clicks apply, zero writes happen — pure read-only dry-run. |

## Phase 6 — Ghost assets

| # | Test | Pass criteria |
|---|---|---|
| 6.1 | Offline ≠ gone | Unplug an archive drive. Affected assets show status `offline`; thumbnails, CLIP search, face search, transcripts still work. |
| 6.2 | Open original: friendly error | Clicking "download original" on an offline asset shows "volume X is offline" — not a 500. |
| 6.3 | Remount auto-resurrects | Plug the drive back in. Within one sidecar poll cycle, assets flip back to `online`. No re-scan, no re-hashing. |
| 6.4 | State transitions are logged | `online → offline → resurrecting → online` transitions appear in the sidecar log with asset counts. |

## Ad-hoc smoke checks (any time)

- `docker compose ps` on `vv` — all 4 containers `healthy`.
- `df -h /volume1` — headroom left (flag at < 20 %).
- `/volume1/faeton-immi/library/` free-space trend (graph in DSM Resource
  Monitor) — not growing unexpectedly fast.
- `docker compose logs immich-server --since 1h | grep -i error` — empty.
- One manual photo upload round-trip, then delete.

## Failure drills (run at least twice a year)

- **Cold restore.** Spin up a scratch NAS/VM, restore the latest `pg_dumpall`
  + `library/` tarball, confirm Immich comes up with faces + albums intact.
- **Mac died, buy new one.** Pretend the Mac is gone. Confirm the Syno-only
  path still serves browsing, search, upload. (Phase 1 fallback path.)
- **Ransomware-ish.** Delete a day's folder under `library/`. Confirm Hyper
  Backup / external snapshot can restore just that folder.
