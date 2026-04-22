# Active implementation plan

Short-horizon spec for the next batch of `immy` tools. Written so a future
session (human or agent) can pick up and build without re-deriving the design.

**Scope.** Four tools under the "external library matching" umbrella: pull a
compact snapshot out of Immich, then find duplicates / near-duplicates on any
external disk or folder, and seed Immich people from Apple Photos.

**Why here and not `docs/PLAN.md`.** `docs/PLAN.md` is the long-form phased
narrative. This file is a focused build spec for the current push. Once these
ship, roll the summary into `docs/PLAN.md` Phase 7 and delete this file (or
replace with the next push).

**Status.** Tools 1 (`snapshot`), 2 (`find-duplicates`), and the **dry-run**
half of tool 4 (`apple-people`) are **shipped**. Remaining: `--apply` path
for tool 4 (gated on a sensible match rate from a fresh snapshot), and
tool 3 (`find-similar`, deferred until tools 1+2 have seen real use).

---

## 1. `immy snapshot` — extract Immich library index ✅ shipped

Dump a compact, self-contained SQLite file with everything needed to
recognise "is this file already in Immich?" from any other machine, even
offline.

### Output

`~/.immy/library-snapshot.sqlite` (override with `--out PATH`).

Single table, no foreign keys, no indices beyond what's below:

```sql
CREATE TABLE assets (
  asset_id     TEXT PRIMARY KEY,        -- Immich UUID
  filename     TEXT NOT NULL,           -- originalFileName, basename only
  size_bytes   INTEGER NOT NULL,        -- fileSizeInByte
  checksum     BLOB,                    -- raw 20-byte SHA1, may be NULL
  taken_at     TEXT,                    -- ISO8601 dateTimeOriginal, may be NULL
  asset_type   TEXT NOT NULL,           -- 'IMAGE' | 'VIDEO'
  library_id   TEXT                     -- for multi-library setups
);
CREATE INDEX idx_filename_size ON assets (filename, size_bytes);
CREATE INDEX idx_checksum      ON assets (checksum);
```

Plus a `meta` table with snapshot timestamp, Immich server URL, asset count,
schema version.

### Source

Query `assets` + `asset_exif` join via existing `PgSink` connection. Roughly
100k rows → ~10 MB SQLite file. `checksum` is stored in Immich as base64
TEXT; decode to 20-byte BLOB to save ~40 %.

### CLI

```
immy snapshot [--out PATH] [--library UUID]
```

No `--apply` / `--dry-run` split — this is read-only on the Immich side.

### Tests

- Round-trip: seed fake `PgSink` with 3 assets, run snapshot, open SQLite,
  assert rows match.
- `meta` table populated.
- Checksum decode is symmetric.
- Empty library produces valid empty snapshot (not a crash).

### Effort

~200 LOC + ~50 LOC tests. Half a day.

---

## 2. `immy find-duplicates` — exact-match locator ✅ shipped

Scan a directory tree on any disk, report which files are already in the
Immich library according to a snapshot.

### Matching tiers

```
exact       filename + size match AND checksum verified equal
likely      filename + size match, checksum not computed (large file, fast mode)
name-only   filename matches but size differs  →  suspicious: re-export? edit?
```

Default mode: compute checksum only when `filename + size` hits a snapshot
row. This keeps us from reading terabytes of non-matching data. `--fast`
skips the hash confirmation entirely (everything lands as `likely`).
`--thorough` hashes everything, even non-matches (slow; only when you
suspect renames).

### CLI

```
immy find-duplicates <path> [--snapshot ~/.immy/library-snapshot.sqlite]
                            [--out ./dupes.md]
                            [--fast | --thorough]
                            [--min-size 1048576]        # skip tiny stuff
                            [--ignore '*.DS_Store']     # glob, repeatable
```

### Report format

Markdown table grouped by tier, with totals. Each row: local path, matched
Immich asset_id, matched filename, size, verdict.

```markdown
## Summary
- 2,341 files scanned, 8.4 TB total
- 1,872 exact matches (7.9 TB — safe to delete locally)
-   412 likely matches (480 GB — verify before deleting)
-    34 name-only matches (12 GB — investigate)
-    23 no match (8 GB — candidates for ingest)

## Exact matches
| Local path | Asset ID | Size |
|---|---|---|
| …
```

Emits a sibling `dupes.json` for programmatic follow-up (`immy ingest` on
the no-match rows, etc.).

### Walk rules

- Honour `--ignore` globs (default: `.DS_Store`, `Thumbs.db`, `*.lrcat-*`).
- Skip macOS bundles (`*.photoslibrary`, `*.aplibrary`, `*.app`) unless
  `--into-bundles`.
- Follow symlinks? No, by default. Flag `--follow-symlinks` if needed.

### Tests

- Fixture tree with 3 matching files, 1 size-mismatch, 1 missing → correct
  tier counts.
- Snapshot with no checksum (NULL) → files still classify as `likely`, not
  crash.
- Empty directory → empty report, exit 0.

### Effort

~250 LOC + ~100 LOC tests. Half a day.

---

## 3. `immy find-similar` — CLIP near-duplicate finder

For files that 1+2 didn't match (different format, re-edited, cropped), find
Immich assets whose CLIP embedding is cosine-close.

### Why separate from `find-duplicates`

1. It needs MLX CLIP on the caller side (external disks won't have it).
2. It's much slower — embeds every candidate file.
3. Output is probabilistic, not binary — different report UX.

### Prerequisites

Snapshot must include CLIP embeddings. Either:
- (a) **Extend `immy snapshot`** with `--with-embeddings` that pulls
  `smart_search` pgvector rows → writes to a sibling `.npy` (512-dim float16
  per asset, ~100 MB for 100k assets). Keeps base snapshot slim.
- (b) Separate `immy snapshot-embeddings` command.

Pick (a) — one command, optional flag.

### Matching

For each local file:
1. Compute CLIP embedding via shared `immy.clip` module (already loaded in
   `process.py`).
2. Cosine against all Immich embeddings; keep top-5 above threshold
   (default 0.90).
3. Report: local path, top matches with score + asset_id + Immich filename.

### CLI

```
immy find-similar <path> [--snapshot …]
                         [--embeddings ~/.immy/library-snapshot.npy]
                         [--threshold 0.90]
                         [--top-k 5]
                         [--out ./similar.md]
```

### Tests

- Synthetic embeddings with known cosine distances → correct ranking.
- Threshold filter excludes low-score matches.
- Missing embeddings file → clear error, not a crash.

### Effort

~300 LOC + ~150 LOC tests. One full day, mostly because of the embedding
export + batched MLX inference on the caller side.

### Deferred until after 1+2

Value is strictly "find edits/re-exports of things already in Immich",
which is a smaller case than "find exact duplicates across 5 backup disks".
Ship 1+2 first, use them for a month, then decide if 3 is worth the weight.

---

## 4. `immy import-apple-people` — seed Immich faces from Apple Photos

Read `~/Pictures/Photos Library.photoslibrary/database/Photos.sqlite`,
extract people you've already tagged, create matching Immich Person rows,
and attach Immich face embeddings that correspond to the same assets.

### Why this works

You've spent years tagging in Apple Photos. Immich's face recognition can
find the same faces but doesn't know their names. We bridge by:
1. Finding asset overlap via filename + size (reuse tool #1 + #2 snapshot).
2. Pulling Apple's face bounding boxes + person labels for those shared
   assets.
3. Computing/fetching Immich face embeddings for the same bboxes (or just
   averaging Immich's detected-face embeddings per person as a centroid).
4. Creating Immich Person rows with the Apple names and attaching all
   correlated faces.

### Apple Photos schema (Photos.app 10+, macOS 13+)

Relevant tables in `Photos.sqlite`:

- `ZPERSON` — `Z_PK`, `ZFULLNAME`, `ZDISPLAYNAME`, `ZFACECOUNT`,
  `ZMERGETARGETPERSON`.
- `ZDETECTEDFACE` — `ZPERSON` FK, `ZASSETFORFACE` FK, `ZCENTERX`, `ZCENTERY`,
  `ZSIZE`, `ZQUALITYMEASURE`, `ZMANUAL` (1 if human-confirmed).
- `ZASSET` — `ZUUID`, `ZORIGINALFILENAME`, `ZORIGINALFILESIZE`.

Read-only. All columns prefixed `Z`. Some foreign keys resolve lazily — test
with `osxphotos` first or pin schema with a fixture DB.

### CLI

```
immy import-apple-people [--photos-db ~/Pictures/Photos\ Library.photoslibrary]
                         [--snapshot ~/.immy/library-snapshot.sqlite]
                         [--dry-run | --apply]
                         [--min-faces 3]         # skip people with <3 confirmed faces
                         [--only 'Name,Name']    # restrict to explicit list
```

Default `--dry-run`: prints the plan. `--apply` hits Immich REST.

### Plan output

```
Found 47 Apple Photos people with ≥3 confirmed faces.
Matched 38 to Immich assets via snapshot (80% overlap).

For each person:
  Mama (Apple: 1,204 faces → 892 in Immich via filename+size match)
    → CREATE Immich Person "Mama"
    → ATTACH 892 face embeddings
  ...

Total: 38 new persons, 14,302 face attachments.
Run with --apply to execute.
```

### Immich API

- `POST /api/people` → `{name}` → returns person_id.
- Face attachment: Immich stores face embeddings on `asset_faces`. Options:
  - (a) Update existing detected faces — set `personId` where `assetId` ∈
    matched + bbox overlaps Apple's bbox (IoU > 0.3).
  - (b) If Immich hasn't detected a face Apple tagged, skip it (don't
    invent). Still a win on the ~80% overlap.

Prefer (a). Direct SQL through `PgSink` is cleaner than fighting the REST
endpoint for this.

### Tests

Needs a fixture `Photos.sqlite` with 2-3 people and 5-10 faces. Either
commit a tiny one or build one in `conftest.py`. Tests:
- Parse Apple DB → expected people list.
- Match against fake Immich snapshot → expected overlap.
- Dry-run prints plan without side effects.
- Apply writes correct `asset_faces.person_id` rows (in-memory SQLite
  double).

### Effort

~400 LOC + ~200 LOC tests + fixture. Full day, possibly 1.5 if Apple schema
surprises us.

### Caveats

- Apple breaks `Photos.sqlite` schema every couple of macOS majors.
  Document the version we tested on; fail loudly on unknown schema.
- Merged persons (`ZMERGETARGETPERSON` != NULL) — follow the merge chain,
  use the target's name.
- Hidden / deleted faces (`ZINTRASH=1`) — skip.

---

## Build order

1. `immy snapshot` (tool 1) — foundation for all the others.
2. `immy find-duplicates` (tool 2) — exercises the snapshot in anger, shakes
   out bugs.
3. Use them for real on at least one external drive before building 3 or 4.
4. `immy import-apple-people` (tool 4) — higher value than tool 3 if Apple
   Photos tagging history is rich, and reuses the same snapshot infra.
5. `immy find-similar` (tool 3) — last, once we know whether near-dup
   matching is actually needed.

## Shared infrastructure

- New module `immy/src/immy/snapshot.py` — SQLite schema, write-side (tool
  1) and read-side helpers (tools 2, 3, 4).
- Reuse `immy.clip` for tool 3.
- Reuse `PgSink` for tools 1 and 4.
- No changes to `process.py` / `cluster.py` / `bloat.py`.

## Out of scope

- Writing *back* into Apple Photos (one-way: Apple → Immich).
- Non-SHA1 hash families (Immich only has SHA1).
- Syncing Apple albums, keywords, or favourites — just people this round.
- Video face tagging from Apple (Apple doesn't tag video faces anyway).
