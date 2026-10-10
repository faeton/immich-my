# Sidecars that contradict their originals

Immich reads a registered XMP sidecar's `DateTimeOriginal` and GPS **over** the
original's own. That's how immy fixes a file it may not touch, but it also means
a wrong sidecar silently breaks a good file. `immy sidecars check` finds and
repairs those sidecars, and promote applies the same rules before writing one.

## What goes wrong

Two bugs in promote's write-back of companion-JSON corrections did this until
2026-10-02. The fixed code stopped making new ones, but nothing repaired the
sidecars already written.

| Bug | Example | What you see |
|---|---|---|
| **Hemisphere dropped**: GPS written as `abs()` | Fiji `−17.8, 177.4` → `17.8 N`; Las Vegas `36.1, −115.2` → `115.2 E` | Map pin in the North Pacific or China; city "Nanle, China"; a time zone like `Pacific/Majuro` or `Asia/Shanghai` |
| **UTC clock, no offset**: a video's QuickTime `CreateDate` (UTC) written as wall time | Las Vegas 15:08 → `23:08:04` | Hours off on the timeline; in the Pacific, the wrong day |

Ray-Ban Meta videos store their capture time as UTC with a `Z`; Apple stores
local time with an offset in `CreationDate`. Both were flattened the same way.

## The rules

`immy/src/immy/sidecar_check.py`, `plan()`:

1. **Position.**
   - The file has its own GPS, and the sidecar's is the same point with a sign
     lost → take the file's.
   - Only the sidecar has GPS, it sits ≥ 300 km from your nearest-in-time
     shot (within 14 h, with its own GPS, no sidecar), and its mirror image
     sits ≤ 60 km from it → un-flip.
2. **Clock.** Only a date without an offset that equals the file's own UTC
   instant (the bug's exact signature) is rewritten:
   - with the file's local time and offset when it has one (Apple);
   - else in the zone at the (corrected) position;
   - else in the zone most of your shots within 3 h carry;
   - else left alone.
3. **Anything else is deliberate.** A location moved in Photos (km away, not a
   mirror) or a camera clock fixed by hours and minutes is never undone.

## Videos on the UTC clock with no sidecar

A video with no GPS and no sidecar, whose file only knows a UTC instant
(QuickTime `CreateDate`; Ray-Ban Meta's `Z` time), is shown by Immich on the
UTC clock. A morning clip in Mumbai shows 04:52 instead of 10:22. With
`--zones` (the default) the check gives each such video its own sidecar,
`name.ext.xmp` (never a shared stem). It carries the true local time:
- from the file's own offset when it has one (Apple);
- else from the zone a clear majority (two thirds) of your shots within 3 h
  carry.

The vote is counted by offset at that moment, so `Pacific/Honolulu` and
`UTC-10` are one vote. A shot votes when its zone is evidence: placed by its
own GPS (a UTC there is real, Lisbon in winter), or an offset from its own
file. A bare `UTC` on a shot with no position means "unknown" and doesn't
vote.

Nothing is written:
- when the vote is split (border days), or when UTC wins;
- for cameras whose QuickTime clock is local (Insta360);
- over an existing unregistered `name.ext.xmp`.

A video that only lacks a zone is looked at again on the next run, since the
shots around it may arrive later.

Several assets can share one sidecar (a Live Photo's HEIC and MOV share
`IMG_1234.xmp`). The repair must be right for every one of them, including a
still that knows only its wall clock, or that sidecar is skipped and reported.

Sometimes (only with `--split`) the files sharing a stem are not one moment: `IMG_5642.HEIC` is a
photo taken ten hours after the clip `IMG_5642.MOV`, or DJI writes `.MOV` and
`.mov` clips with one name. Then the stem-named sidecar is split per file:
- **Each file it describes** (by its own date and GPS, allowing for the bugs
  above) gets a copy as `name.ext.xmp`, repaired if needed.
- **Each file it doesn't describe** gets a minimal `name.ext.xmp` of what the
  file says about itself (`sidecar_check.own_patch`). That is its capture time
  with an explicit offset (a UTC instant in the zone at its GPS or of the shots
  around it, else `+00:00`), plus signed GPS. It is never exiftool's generic
  copy, which would carry QuickTime's UTC `CreateDate` without an offset.
  Immich prefers a full-name sidecar over a stem match, so the stem file never
  re-attaches.

A file with nothing to compare, or nothing of its own to say, blocks the
split. Splitting is opt-in (`--split`): a mismatch can also be a deliberate
correction (one camera's clock fixed to match another's). The routine run
reports these as "assets disagree"; review them with `--csv` and split
deliberately. A split cut short is finished by the next run
(`sidecar-check-splits.txt`). The old `name.xmp` stays on disk,
unregistered.
A date with an explicit offset is right for both. A sidecar that another
user's assets also use is skipped.

Cameras whose QuickTime clock is local, not UTC (Insta360, by make or file
name; `capture.QUICKTIME_LOCAL_CLOCK_MAKES`), are never treated as a UTC clock.
A position is only un-flipped in the direction `abs()` breaks it (a lost
minus), and never when the mirror is within 300 km (near the equator or a
zero meridian a mirror can be a real move).

## Running it

```sh
immy sidecars check --originals /path/to/library --csv plan.csv   # dry run
immy sidecars check … --asset <id> --apply                         # pilot one
immy sidecars check … --apply
```

- Only new or changed sidecars are read on each run. The last good state of
  each is kept in `sidecar-check.json` under `state_root`. `--all` re-checks
  everything.
- `--apply` rewrites only the wrong fields.
- Every repaired sidecar's previous text goes to
  `sidecar-check-<time>.jsonl`.
- A metadata refresh is queued for exactly the repaired assets, plus any
  sharing their sidecar. Refresh intent is appended to
  `sidecar-check-pending.txt` before each sidecar changes, so an interrupted
  run or an API failure is retried next time.
- An original exiftool can't read is skipped and retried, never taken as "no
  metadata".

It is the last stage of the ingest: `deploy/n5/photos-ingest.sh --promote`
runs it after `promote-rest --write`.
