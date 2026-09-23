# Phase 2 — manifest identity (schema v3 → v4)

**Status:** rev 3, implemented and code-reviewed 2026-09-23 (`dedup/identity.py`, `dedup/manifest.py`,
`dedup/engine.py`). Two Codex design reviews shaped it; rev history at the bottom.
Parent: `todo/PHOTOS-BRIDGE-REVIEW.md` P1. n5-only work — nothing here needs the Mac.

## Problem

The manifest keyed every row on `path` alone:

1. The same original arriving by a second route (icloudpd today, a Photos export tomorrow)
   was a new row, caught only if the heuristic cascade re-discovered it while the first copy
   was still clusterable. A `promoted` row never re-enters clustering, so the forward edge
   re-promoted.
2. After promotion the manifest did not know where the file went (`asset.path` names the
   gone staging file) and kept no content hash.

## Principle

A staging file may be disposed of as a duplicate only when **the library, right now, holds
the exact same bytes** — proven by a full sha256 of a real, non-symlinked file that is not
the staging file itself, re-checked immediately before the move. Index rows and `source_uid`
matches find candidates; they never authorise a disposal on their own.

## Schema v4

`library_file (path PK, bytes, mtime_ns, inode, sha256, hashed_at)` — what the library holds,
by content, independent of asset rows. `(bytes, mtime_ns, inode)` is a cache key for skipping
re-hashes, **not** proof (triage deliberately preserves mtime on a swap, and deletes the row
itself).

New `asset` columns: `source_uid`, `component` (required whenever `source_uid` is set),
`sha256`, `dest_path` (written before the source is unlinked), `alias_path`.
Index `(source, source_uid, component) WHERE source_uid IS NOT NULL` — not UNIQUE; the
resolution logic decides what a repeated UID means.

New status `alias` → disposed by `dedup apply` → `quarantined` (`alias_path` kept).

Migration: `_CREATE_SCHEMA` (includes the new columns/table; commits) → if version missing or
`< 4`: `BEGIN IMMEDIATE`, add each missing column (`PRAGMA table_info`), bump version with a
bare upsert, `COMMIT` / `ROLLBACK` → always `_CREATE_INDEXES`. On a copy of n5's live
manifest (285k rows): < 1 s, integrity ok.

## What counts as a library file

`identity.library_eligible`: media extension, name not starting with `.` (covers
`.<name>.<pid>.partial` and triage's `.immy-new.<name>`). The walker prunes hidden
directories. `hash_stable` refuses symlinks and non-regular files and stats before and after
the read (`UnstableFile` if size/mtime_ns/inode/dev moved). `library_match` drops rows whose
file is gone/symlinked/ineligible, re-hashes rows whose stat changed, and never returns a file
with the staging file's own (dev, inode) — a hardlink or bind-mount view is not a duplicate.

## Fingerprint (`identity.resolve`, per asset, sequential on one connection)

```
source_uid without component          -> error
0 bytes, or exiftool ExifTool:Error / File:Error (not warnings) -> error "stub: …"
sha, st = hash_stable(file)           (unstable -> error)
source != 'originals' and library_match(sha, not_same_as=file) -> alias (no pHash)
source_uid and a promoted/canonical twin with a KNOWN, DIFFERENT sha -> error "revision of #N"
otherwise                             -> fingerprinted (+ sha256, uid, component)
originals rows                        -> also upserted into library_file (bootstrap builds the index)
```

`write_fingerprint` is conditional on `status='registered'` and reports whether it wrote.

**No UID deferral.** A UID twin still in flight is fingerprinted normally; the cascade
(identical bytes, same ContentIdentifier) handles it as before v4. Rev 2's "defer" needed an
owner-election protocol to guarantee progress, for no gain in safety — disposal already rests
on library content.

## Moves (`_move_asset`, used by promote-rest, apply, alias disposal)

```
dest, done = _resolve_dest(base, id, bytes, src, recorded=(dest_path, sha256))
  recorded present: src present and sha(src) != recorded -> refuse (never consume new bytes)
                    recorded file hashes to recorded      -> done
                    else, src gone                        -> refuse (no size guess)
  src and dest are one file (samefile)                    -> refuse
  dest free -> use it; dest holds src by FULL sha256 -> done; legacy (src gone, nothing
  recorded, pre-v4 crash) -> size fallback, deletes nothing; else collision name
not done: _safe_copy = copy → fsync file → rename → fsync dir → full sha256 re-read
record dest_path + sha256 (+ library_file if under originals_root); COMMIT
unlink src; fsync src dir
caller: status; COMMIT
```

Every candidate destination (recorded, plain, collision name) must be a regular file, not a
symlink, and not the source's own inode. Before the record is written — on every path,
including `already_done` recovery — the destination file and each directory up to the move's
root are fsynced (`_make_durable`); fsync errors propagate, so a failed sync keeps the source.

**Source-immutability assumption.** Before unlinking, the source's (dev, inode, size,
mtime_ns) must equal what it was when the move began. That detects replacement and ordinary
rewrites; it does not prove current content (an in-place same-size rewrite restoring
mtime_ns would pass). Staging files are assumed settled while a mover runs — sources land
files by rename, the settle gate and the movers lock keep immy's writers apart. A second full
hash would not remove the hash→unlink window without writer coordination.

`content_equal`'s sampled windows remain clustering evidence only.

All file movers — `dedup apply`, `dedup promote-rest`, `triage apply` — take one lock,
`<manifest>.movers.lock`.

## Alias disposal (`_dispose_aliases`, end of `dedup apply`)

Shares `--limit` with the cluster queue; counted in dry-run. Immediately before each move,
`identity.still_holds` re-hashes **both** files in full and checks they are different files.
Fail → row back to `registered`, `alias_path` cleared, the next fingerprint pass decides it
afresh. A row that already has `dest_path` (copied by a crashed prior run) resumes through
the recorded evidence.

## Commands

- `immy dedup index-library --originals /originals [--dir 2026/05 …] [--limit N]` — hash
  library subtrees into `library_file`; resumable; prunes vanished files within the walked
  subtrees only. For the bridge overlap window, index recent `YYYY/MM` dirs, not 4 TB.
- `immy dedup retry-errors [--match stub]` — `error` → `registered`, size/mtime refreshed;
  missing files stay `error`.

No per-row history backfill: pre-v4 promoted rows keep `dest_path`/`sha256` NULL. A guessed
location is not proof of identity, and the library index answers the only question disposal
needs.

## Decisions

1. Revisions not modelled in v1 — held as `error: revision of #N`.
2. Aliases are quarantined, never deleted, and only on disposal-time proof.
3. Every new arrival is hashed, all sources (one extra read for videos).
4. No history reconstruction.

## Known limits

- Freshness between index time and use is heuristic; safety comes from the disposal-time
  re-hash, not from the index.
- Durability: file and directory fsyncs are in place; ZFS/NFS attribute-cache behaviour on n5
  has not been measured.
- Triage's executor clears `asset.sha256` and the `library_file` row on a swap; any other
  in-place writer to the library must do the same (or rely on the disposal-time re-hash).

## Where the UUID comes from (finding, 2026-09-23)

osxphotos 0.77.1 source: `--sidecar json` is exiftool-format metadata with **no UUID**. The UUID
is in the JSON export report (`--report batch.json`, key `uuid`; the CSV report drops it) or can
be written per file with `--sidecar-template`. Phase 3 reads the report; component is inferred
from filename/extension (live `.mov`, RAW ext, `_edited` suffix).

## Revision history

- **rev 1** — aliased to asset rows; backfilled promoted rows' destinations by re-deriving
  `_promote_dest`. Codex: guessed locations are not identity; bootstrap could alias the library
  itself; sampled equality could authorise an unlink; UID matches could dispose of files whose
  twin was not in the library.
- **rev 2** — library content index; record-before-unlink; UID defers. Codex: recorded-dest
  recovery could delete a changed source; aliases must be re-proven at disposal; symlinks/temp
  names/hardlinks; UID deferral had no progress guarantee; durability and cross-writer locking.
- **rev 3** — all of the above addressed as described; UID deferral dropped. Two Codex code
  reviews then found: symlinked/same-inode destination candidates, unchecked source
  consumption after a fresh copy, swallowed fsync errors and non-retry-safe durability, alias
  recovery skipping re-proof, a stale-index alias→requeue loop, hidden/symlinked-ancestor
  library paths, and unconditional error writes. All fixed with regression tests (801 pass).
