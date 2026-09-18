# Photos Bridge — review findings & action plan

**Status:** P0.1–P0.4 fixed (Phase 0.5 done, 2026-09-18). Everything else open.
**Date:** 2026-09-18
**Subject:** the "Photos Bridge" design brief (Apple Photos → `osxphotos` → rsync batches
→ `immy dedup` → Immich external library), which supersedes the `icloudpd` forward-sync path.

Three independent passes produced this: a code-level verification against the repo, a
second Codex (GPT-5.4) pass aimed at the dedup cascade and the migration, and a Grok pass
aimed at design judgement. Every code claim below was re-verified by reading the source;
`file:line` refs are current as of commit `4f4df6a`.

**Verdict:** the architecture is right — `osxphotos` as the source, immutable batches
instead of a replica, real identity instead of path identity. What is wrong sits underneath
it: three live correctness bugs in the existing pipeline, several `osxphotos` flags that do
not exist, and a transport protocol that does not deliver the guarantee it claims.

---

## P0 — Live bugs. Fix independent of whether the bridge is ever built.

These are reachable today, with no new source. They are not in the brief.

**All four are fixed as of 2026-09-18** — `dedup/engine.py`, covered by
`immy/tests/test_dedup_safety.py` (20 tests, no pyvips needed). Each fix is
marked below; the diagnosis text is left as written, since it is the record
of what the code did. No schema change and no new source, per Phase 0.5.

### P0.1 `_resolve_dest` can delete an asset that never reached the library

`dedup/engine.py:978`, callers at `engine.py:1093-1097` and `engine.py:1177-1184`.

`_resolve_dest()` treats an existing destination of the expected size as a completed prior
move and returns `already_done=True`. Both callers then run `elif src.exists(): src.unlink()`
without ever calling `_safe_move`.

Failure: the library already holds `originals/2026/09/IMG_1234.JPG`; a different asset maps
to that same `YYYY/MM` + basename and happens to share a byte length. Its staging file is
deleted and its row marked `promoted`, but its content never reached the library.

The docstring already anticipates the "occupied by a DIFFERENT asset" case — it just uses
size as the discriminator, which is too weak. Populating `sha256` in the manifest does **not**
fix this; the bypass is downstream of identity.

**Fix:** require verified content equality (hash, or at minimum a sampled compare) before
returning `already_done`, for both the plain and the asset-id-qualified destination.
Severity: silent data loss in a pipeline that has already run. This is the single most
urgent item in this document.

**Done 2026-09-18.** `_resolve_dest` now takes the staging `src` and defers to
`_dest_holds_this_asset`, which requires `content_equal(src, candidate)` on both the
plain and the qualified name. The one remaining size-only branch is `src` already
consumed — nothing is left to compare and nothing is deleted there, so its worst case
is a bookkeeping row on the wrong twin, never a lost file.

### P0.2 Same-stem, same-size videos auto-merge on filename alone

`dedup/engine.py:465` (early return), `engine.py:762` (`_metadata_agrees` fallback),
`engine.py:903` (`_decide_one` video branch).

`_pair_evidence` returns `("strong", None)` on `a.bytes == b.bytes` **before** reaching the
`VIDEO_STEM_PLAUSIBILITY_SECONDS` gate — the gate added 2026-07-12 for exactly this failure
sits behind the early return it was meant to protect. `_decide_one`'s video branch only tests
`member.bytes != winner.bytes`, and `_metadata_agrees` falls through to
`normalized_stem(a.path) == normalized_stem(b.path)`. So two unrelated `IMG_1234.MOV` of equal
byte length auto-merge, and one gets quarantined. Neither path consults `live_cid`.

Live Photo `.mov` halves make exact size collisions substantially likelier — thousands of
~3 s clips at one resolution and bitrate — so the bridge sharpens an existing edge.

**Fix:** require real hash equality for the exact-video shortcut; treat conflicting `live_cid`
as a hard bar to heuristic merging.

**Done 2026-09-18.** The equal-size shortcut in `_pair_evidence` confirms content before
claiming `strong` and otherwise falls through to the plausibility gate; `_decide_one`'s
video branch confirms content after the size check; an unreadable file counts as *not*
confirmed. Conflicting `live_cid` returns `review` for any cluster, image or video.

### P0.3 An extended cluster keeps a stale CLIP score

`dedup/engine.py:517` (in-place merge), `engine.py:563` (`clip_cos_sim IS NULL` filter),
`engine.py:897` (`decide` consuming it).

`cluster()` merges new members into an existing `cluster_id` rather than creating a new
cluster. `_clip_ready_clusters` only selects clusters where `clip_cos_sim IS NULL`, so an
extended cluster is never re-scored, and `decide()` applies a cosine computed over different
membership. A newly-joined member at Hamming 10 inherits a `0.999` earned by two other images
and is marked a loser.

`originals` rows deliberately re-enter clustering (`engine.py:482`), which is the bridge that
lets a fresh arrival attach to a settled cluster.

**Fix:** invalidate `clip_cos_sim` whenever membership changes; recompute over the full
membership before permitting `auto`.

**Done 2026-09-18.** `cluster()` tracks whether a reused cluster actually gained rows and
clears `clip_cos_sim` when it did, scoped to `pending`/`review` clusters (an `auto`
cluster has been acted on and `decide()` never revisits it). Stage C re-queues the
cluster; until it runs, `_decide_one` sees `clip_cos=None` and routes to review.
`dedup cluster` prints the extended count.

### P0.4 RAW/JPEG companion exclusion does not survive transitivity

`dedup/engine.py:425` (`_is_raw_jpeg_companion`), `engine.py:439` (only consulted for the
direct edge), `engine.py:861` (`_decide_one` has no companion check at all).

The exclusion suppresses only the direct RAW↔JPEG edge inside `_pair_evidence`. Union-find
still joins both components through any third image.

Scenario: same-directory `IMG_1234.DNG` + `.JPG`, both 6000×4000, plus a canonical 3000×2000
JPEG of the same capture. Both match the canonical on pHash and timestamp. Differing
dimensions clear the burst guard; identical aspect ratios clear the crop guard. The canonical
wins and **both Photos components, including the irreplaceable RAW, become losers.**

Needs a third image to fire, so rarer than P0.1–P0.3 — but the bridge makes RAW+JPEG a
constant arrival pattern rather than an occasional one.

**Fix:** component incompatibility must survive transitive clustering and be re-checked in
`_decide_one`.

**Done 2026-09-18.** `_decide_one` re-checks `_is_raw_jpeg_companion` over every member
pair before winner selection and returns `review`. Deliberately not persisted: the
predicate is a pure function of path and format, so recomputing it at decide time cannot
drift from what pairing saw.

---

## P1 — Identity (brief §6). Worth doing even if the bridge dies.

All three reviews agreed on this independently: path-only
`INSERT … ON CONFLICT(path) DO NOTHING` (`dedup/manifest.py:198`) is already wrong for a
multi-source library. Corrections to the brief as written:

- **Schema version is 2, not 3.** `manifest.py:31` — `SCHEMA_VERSION = 2`. The migration is
  **v2 → v3**, not v3 → v4.
  *Superseded 2026-09-18:* merging the triage branch took `SCHEMA_VERSION` to **3**
  (`triage` + `video_signal`), so the identity migration is **v3 → v4**. The point stands —
  read the constant, do not assume. The v3 step also sets the precedent the next one should
  not copy: it is table-creation only and leans on `_CREATE_SCHEMA` having already run, so
  `_migrate` has no restart-safety story to inherit.
- **The DDL contradicts the pseudocode.** §6 promises "else → new REVISION; record, do not
  silently overwrite", but `UNIQUE (source, source_uid, component)` permits exactly one row
  per tuple and there is nowhere else to put a revision. Two successive edited renders of one
  UUID collide on insert. Either add a revision table or drop the promise — not both.
- **The stated reason for the partial index is wrong.** SQLite UNIQUE indexes already permit
  unlimited NULLs, so 270k legacy rows would *not* "all collide on NULL". Keep the partial
  predicate (it keeps the index small), fix the rationale.
- **The migration is not restart-safe as printed.** `_migrate()` commits separately from
  `set_meta("schema_version")` (`manifest.py:113`, `manifest.py:149`). An interrupt between
  them leaves partially-added columns recorded under the old version; the retry dies on
  "duplicate column name". The existing v1→v2 step dodges this by inspecting
  `PRAGMA table_info` first — preserve that, or transact the DDL and version bump together.
- **Index placement breaks old manifests.** `_CREATE_SCHEMA` runs at `manifest.py:133`,
  *before* the version check. Putting the new indexes there makes every pre-v3 manifest fail
  at `executescript` before `_migrate()` can add the columns they reference. New-column
  indexes must be created inside/after the migration.
- **Identity resolution cannot live in `register`.** `source_uid` arrives in the sidecar,
  which is only read at fingerprint time; `register` is deliberately fs-only (`cli.py`:
  "fast, fs-only"). So UUID dedup belongs in fingerprint — which needs a new terminal status
  (`write_fingerprint` only ever moves to `FINGERPRINTED`) *and* an explicit disposal of the
  staging file, or `ready/` never empties and the §11 "ingest stalled >24 h" alarm fires forever.
- **The sha256 alias branch cannot fire at launch.** The 222k already-`promoted`/`canonical`
  rows never pass through `_safe_move` again, so they never acquire a hash — and that branch
  is precisely what was meant to stop re-promoting the forward edge. Needs an explicit
  backfill over the **overlap window only** (~2026-05 → present), not 4 TB.
  Mitigating context the brief undersells: `dedup bootstrap` already seeds `originals` as
  `canonical`, `load_clusterable` includes canonical rows, and `_decide_one` routes any
  cluster that would displace a canonical to review. The net exists; it is heuristic.

---

## P2 — `osxphotos` reality check (brief §4)

Verified against the installed package.

- **`--live-photo`, `--raw-photo` and `--edited` do not exist.** Those behaviours are
  **defaults**; the opt-outs are `--skip-live` / `--skip-raw` / `--skip-edited`. Remove them
  from the command in §4.
- **`--added-after` does exist** for `export` (via the shared `@QUERY_OPTIONS` decorator), and
  the reasoning for it over `--from-date` (date *added* vs capture date) is correct.
- **`osxphotos` on m3max is currently broken.** 0.68.6 installed under Python 3.9 user-site;
  it crashes on import — `TypeError: unsupported operand type(s) for |` at
  `osxphotos/bookmark.py:12` (PEP 604 union under too old an interpreter). Reinstall on a
  supported Python and pin the version. **This is a Phase 0 blocker the brief does not list.**
- **`--skip-original-if-edited` already exists**, as does `--export-aae`. §7's option 3 is a
  one-flag change, not "the most work and hardest to reverse".
- **`--only-new`** — "ignores any previously exported files, even if missing from the export
  folder" — is the escape hatch for the unbounded Mac export tree (see P3).
- **`--sidecar` accepts `xmp`, `json`, `exiftool` and is repeatable.** Running
  `--sidecar xmp --sidecar json` is nearly free and closes brief §14.3: the XMP template
  carries `dc:subject` (keywords), `Iptc4xmpExt:PersonInImage`, `photoshop:DateCreated` and
  `exif:GPS*`, which Immich reads directly for images. The XMP has **no UUID field**, hence
  JSON as well. Caveat: videos never read XMP (that is why `tags sync` exists), so Live `.mov`
  and video keywords still need the Tag API path.
  Naming: immy writes `basename.xmp` (`sidecar.py:25`); `osxphotos` defaults to
  `basename.ext.xmp`. `--sidecar-drop-ext` aligns them but then a RAW+JPEG pair collides on
  one stem — keep the default and let immy handle both forms.

### Source-adapter wiring gaps

Small, concrete, easy to miss:

- `SOURCE_WEIGHT = {"originals": 120, "icloud": 100, "google": 30}` (`engine.py:96`), read via
  `.get(a.source, 50)` (`engine.py:706`). A `photos` source defaults to **50** and therefore
  **loses winner selection to any icloudpd copy.** Should be ≥ 110.
- `_rescue_sidecar` is gated on `source == "google" and taken_src == "json"` in **both** promote
  paths (`engine.py:1100`, `engine.py:1189`). Photos-source corrected dates and GPS would land
  in the manifest and never reach Immich. Make the gate `taken_src == "json"`, source-agnostic.
- `_EDITED_NAME_RE = r"-(edited|effects)$"` (`engine.py:105`) uses a **hyphen**, but
  `osxphotos`' default `--edited-suffix` is `_edited`. Edited renders would arrive with
  `edited=0` and lose the never-auto-merge guard. Fix with `--edited-suffix '-edited'` (zero
  code) or widen the regex.
- `dedup/review.py:427` styles `.src.icloud/.google/.gdrive/.originals` — add `.photos` or the
  new source renders unstyled in the review UI.

---

## P3 — Transport (brief §5). Two guarantees that are not actually provided.

### P3.1 A failed transfer disappears from the next run's report

Export commits to `.osxphotos_export.db` at step 1. If the transfer fails at step 4, the
watermark correctly stays put — but that does not undo the exporter's committed state. On the
next pass those files are already up to date locally, so they report as **`skipped`**, a
column distinct from `exported`/`updated` (`photoexporter.py:925`, `cli/report_writer.py:91`).
§5 step 1 selects only `exported` or `updated`, so they are **never selected again** and never
reach n5.

The brief's claim that "a batch that fails anywhere is simply re-exported next run" is false
as specified.

**Fix:** persist an unacknowledged-delivery list that outlives any single report, and retry it
until publication is acknowledged.

### P3.2 Atomic publication proves the wrong thing

If a Live Photo's still exports but its `.mov` download fails, every *selected* file verifies
and lands in `ready/`, and the protocol permits the watermark to advance. The asset's
*date added* never changed, so subsequent `--added-after` queries exclude it **permanently** —
half an asset, silently, forever. §11 treats export errors as a `warn`.

**Fix:** watermark advancement needs completeness accounting **per asset and its required
components**, not per transferred file — or a durable retry queue that stays eligible
regardless of the watermark.

### P3.3 The Mac export tree grows without bound

"A few GB" is true for one run and false after a year: a persistent tree plus a forward edge is
a full mirror of everything since launch, on a travelling laptop. Pruning breaks `--update`,
which re-exports anything missing from disk. `--only-new` is the escape hatch. Decide now.

### P3.4 `--cleanup` is more dangerous than §8 says

Combined with `--added-after`, the "export set" is just this run's narrow query — `--cleanup`
would delete the **entire** previously-exported tree, not merely assets deleted in Photos. The
prohibition in §8 is right; that should be the stated reason.

### What to keep

Keep `.staging/` → `ready/` — it is a single `mv` and it is what makes a half-transferred batch
un-ingestable. What is genuinely redundant is the **per-file sha256 on both ends**: rsync
already checksums every file it transfers. Cut the double hash, not the directory contract.

---

## Decisions to lock

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | §7 edited renders | **Original only.** Do not promote the render; do not keep a hidden archival namespace | Immich keeps the negative, Photos/iCloud keep the look. Promoting both doubles the timeline for every crop and filter, and Immich will not merge them. Reversible — re-export the UUID set later. Evidence Immich is not the daily viewer: two months of missing phone photos went unnoticed |
| 2 | §8 deletions | **Grow-only default — but not "ever."** Record `deleted_in_photos_at`, never unlink, surface a quarterly "in Immich, gone from Photos" list | Automatic propagation would trash faces, albums and DB-locked `AI:` captions. But an archive that cannot even *notice* deletions becomes a landfill of screenshots and boarding passes. Observation ≠ cleanup |
| 3 | §14.4 backfill depth | **Ingest the 2026-07-13 → present gap only.** Audit the rest by UUID/filename/size **without downloading**; pull only the flagged holes | A full-library sha256 audit is not "cheap and read-only": under Optimize Mac Storage most originals are not on disk, so it is a ~1 TB PhotoKit crawl — exactly what §4 already calls an unacceptable foundation |
| 4 | §2/§14.2 host | **A mini is not merely a latency upgrade.** §2's claim that nothing would need redoing is wrong | launchd + `RunAtLoad` + lockfile + watermark-after-verify exist *only* because the host sleeps in a backpack. The identity work survives a host change; the protocol is what the laptop costs. `osxphotos` as the source is right; m3max as the host is the part to reconsider |
| 5 | Complexity | Cut: per-file sha256 both ends, REVISION as a first-class workflow, 5 of the 7 §11 signals. Keep: identity + stub guard, the `photos` adapter, fixtures, and one "bridge silent **while m3max was on the tailnet**" alert | Hash failure can be a hard exit in the script rather than a pager; duplicate-surge is a test assertion on the first live runs, not a standing monitor; batch-stuck and ingest-stalled collapse into one condition |

---

## Revised phasing

| Phase | Deliverable | Gate |
|---|---|---|
| **0** | Unlock the Apple Account. **Reinstall `osxphotos` on a supported Python and pin it.** Confirm Photos on m3max is synced | prerequisite for everything |
| ~~**0.5**~~ | ~~**P0.1–P0.4 fixed, with tests.** No new source, no schema change~~ | **Done 2026-09-18** — `test_dedup_safety.py`, 20 tests; suite shows no new failures |
| **1** | Fixture corpus (brief §12) exported and characterised | regression suite exists; §7 already decided, so this confirms rather than decides |
| **2** | Schema **v2→v3** + identity logic + `stub` guard + sha256 backfill over the overlap window | migration is restart-safe; alias branch can actually fire |
| **3** | `immy dedup register photos` reading `osxphotos` JSON sidecars; source-adapter wiring gaps closed | fixture batch ingests correctly in a **shadow manifest** |
| **4** | Mac bridge script + scheduler + batch transport, including the unacknowledged-delivery queue and per-asset completeness accounting | a real batch lands in `ready/` and verifies; a deliberately failed transfer is retried successfully |
| **5** | Wire to the live manifest; backfill the 2026-07-13 → present gap only | no duplicate promotions; spot-check in Immich |
| **6** | Monitoring (trimmed); two weeks of clean runs | then and only then, brief §9 decommissioning of the icloudpd stub tree |

Phase 0.5 is new and did not appear in the brief. Phase 2 remains worth doing on its own merits.

**Where this stands (2026-09-18):** Phase 0.5 is complete. Phase 0 is the next gate and is
not a code task — unlock the Apple Account, reinstall `osxphotos` on a supported Python and
pin it, confirm Photos on m3max is synced. Nothing after it can be verified until then, and
the open questions below (especially #1, catalog vs. backup) still decide the shape of
Phases 4–6.

---

## Open questions

1. **Is Immich a catalog, or the backup that licenses deleting from the phone?** The brief never
   says, then borrows backup language. If a catalog, the "sometimes a week late" SLA is merely
   annoying. If a backup, it is not acceptable and the phone needs a hot path. Everything about
   the host decision follows from this answer.
2. **Delete-during-lag.** §8 teaches "Photos deletes don't matter, Immich keeps them" — so the
   user deletes a shot that has not been exported yet. Gone from Photos, never arrives. Recently
   Deleted is 30 days; a week of sleep plus a two-week trip fits inside that. §8 and the SLA are
   individually defensible and jointly dangerous. Decision #2 above mitigates but does not close this.
3. **Travel is the worst case on every axis at once** — photos are unreshootable, cellular iCloud
   is least reliable, m3max is awake but off-tailnet or on a 5–10 Mbps uplink, and the pipeline is
   idle. Worth an explicit answer rather than an implicit acceptance.
4. **Revision storage** (P1) — small side table, or drop the revision promise entirely for v1?

---

## Verification notes

Code claims were checked by reading the source, not inferred: `dedup/engine.py`,
`dedup/manifest.py`, `dedup/review.py`, `cli.py`, `sidecar.py`, `exif.py`.
`osxphotos` claims were checked against the installed 0.68.6 package (CLI option lists,
`photoexporter.py`, `cli/report_writer.py`, `templates/xmp_sidecar.mako`) — **re-verify against
whichever version gets pinned in Phase 0**, since the CLI moves between releases.

P0.2, P0.3 and P0.4 were additionally reproduced by Codex against in-memory fixtures with
filesystem behaviour mocked; no repository files were modified during review.
