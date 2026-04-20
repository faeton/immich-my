# Sidecar internals

Concrete shape of the curator sidecar: where its state lives, how workers
claim and run jobs, and how the processes are laid out across the two
hosts. For the broader architecture and phased rollout, see
[ARCHITECTURE.md](ARCHITECTURE.md) and [PLAN.md](PLAN.md).

## Glossary

- **`immy`** — the curator sidecar CLI (Mac-side, `immy audit` / `immy promote`
  / `immy status`). Wraps `exiftool`, `osxphotos`, and the Immich REST API.
  Audits trip folders against a declarative rule catalogue, applies fixes to
  EXIF + XMP sidecars, then promotes clean folders into Immich's external
  library. Phase 2a; web routes under `/audit` are added in Phase 2a.7.
- **trip folder** — a directory under `~/Documents/Incoming/<TripName>/`. The
  folder name is the event label; camera subfolders are optional. Contains
  a **folder notes file** (see below) and a hidden `.audit/` (machine state).
- **folder notes file** — YAML front-matter (machine-readable: location,
  timezone, coords, tags) plus a free-form body (user travel notes, shoot
  notes, project context). `immy` looks for it by name in this order and
  the first existing file with valid front-matter wins: `TRIP.md` →
  `IMMY.md` → `README.md`. When no candidate exists, `immy` writes
  `README.md` (most universal). If a `README.md` exists without
  front-matter, `immy` inserts the front-matter at the top and leaves the
  body untouched.
- **`.audit/state.yml`** — machine-managed per-rule decisions (clock offsets,
  pair groupings). Never user-edited.
- **promotion** — rsync of a trip folder (plus its `.xmp` sidecars and the
  notes file) from the Mac's `Incoming/` into the NAS's `originals/<TripName>/`,
  followed by a `POST /api/libraries/:id/scan`. The files live on the NAS;
  Immich only reads them.

## Shape at a glance

- One Python package (`sidecar/`), many entry points. Each worker is a
  long-running process; they coordinate only through the database.
- State lives in a dedicated `sidecar` database on the Syno's existing
  Postgres instance — the same server Immich uses, a different database.
- No `pgvector` in the sidecar DB. CLIP and face embeddings stay in
  Immich's own tables where pgvector already lives; the sidecar pushes
  results to Immich via REST.
- Mac workers reach the DB over Tailscale. Syno-side workers reach it
  over the docker network.

## `immy` — metadata forensics CLI

`immy` is the Mac-side entry point. The queue/workers described below handle
post-ingest enrichment; `immy` handles the *pre*-ingest step that gets a trip
folder into `originals/` with metadata already correct.

### Layering

```
              ┌────────────────────────────────────────────────┐
              │  immy engine (Python library)                  │
              │    rules catalogue, exiftool, XMP write,       │
              │    state.yml, JSONL audit log                  │
              └──────────────┬─────────────────────────────────┘
                             │
         ┌───────────────────┼─────────────────────┐
         │                                         │
   ┌─────▼─────────┐                   ┌───────────▼───────────┐
   │ CLI           │                   │ Web routes (Phase 2a.7)│
   │ `immy audit`  │                   │ `/audit/*` on sidecar │
   │ `immy promote`│                   │ (map picker, thumbs)  │
   └───────────────┘                   └───────────────────────┘
```

Engine is source of truth. Both fronts are views on top. CLI for HIGH +
terminal-friendly y/n MEDIUM. Web for LOW-confidence questions that need maps
or thumb grids. Never a web-only tool — cron / watcher needs the CLI forever.

### Rules engine (as shipped)

Rules are Python callables registered into `immy.rules.registry`. Each
rule produces a list of `Finding`s from `(rows, folder)`:

```python
@dataclass
class Finding:
    rule: str                       # unique name, e.g. "dji-gps-from-srt"
    confidence: Literal["high", "medium", "low"]
    path: Path                      # media file the finding targets
    action: Literal["write_xmp", "pair", "note"]
    patch: dict[str, str | list]    # exiftool tag → value (list for XMP lists)
    pair_with: Path | None = None   # for action="pair"
    reason: str = ""
```

Shipped rule set (in registration order — earlier entries are "more specific"
and win the per-field dedup):

1. `dji-gps-from-srt` (HIGH) — GPS from a sibling `{stem}.SRT`.
2. `dji-date-from-srt` (HIGH) — `DateTimeOriginal` from the SRT's first timestamp line.
3. `date-from-filename-vid-img` (HIGH) — `VID_/IMG_/DJI_/MVI_/PXL_YYYYMMDD_HHMMSS`.
4. `insta360-pair-by-ts-serial` (HIGH) — `.insv` ↔ `.lrv` grouped by `(timestamp, serial)` in the filename; recorded in `state.yml` (Immich stack API call lands in 2a.4).
5. `trip-gps-anchor` (HIGH) — `location.coords` from the folder notes file; fires on every GPS-less media.
6. `trip-tags-from-notes` (HIGH) — `tags:` list from notes → `XMP:HierarchicalSubject` + flat `XMP:Subject`.
7. `trip-timezone` (HIGH) — `timezone:` IANA zone from notes → `XMP:DateTimeOriginal` rewritten with `±HH:MM` suffix at each file's capture instant. Respects per-file `EXIF:OffsetTimeOriginal`: when a file already carries its own offset (iPhones, modern mirrorless), that wins over the trip zone — a device that crossed a border mid-trip still has the right offset per shot.
7b. `trip-timezone-guess-gps` (HIGH, `write_notes`) — when notes has no `timezone:` but some media carry GPS, reverse-looks-up the zone via `timezonefinder` (offline, no network) and writes the majority zone into notes front-matter. On the next apply pass, `trip-timezone` cascades the offset into XMP. Stays silent when two zones run close (border-crossing trip → user decides via the interactive prompt).
8. `clock-drift` (MEDIUM) — folder-median coherence check over resolved capture dates; flags files >24 h from the median with source + delta, proposes `DateTimeOriginal = median` as the patch. Needs ≥3 samples; ignores `mtime`-sourced dates as too noisy.
9. `tag-suggest-missing` (MEDIUM, `write_notes`) — diffs existing notes `tags:` against what the scaffold would produce from the *current* folder contents; proposes any tag whose category (prefix before the last `/`) is entirely absent from the user's list. Opt-out: `tag_suggestions: off` (YAML bool `off/false/no` or any string matching). Accepted patches go through the `write_notes` action: the CLI merges the `add_tags` list into notes front-matter (unique, order-preserving), and the next apply pass picks them up via `trip-tags-from-notes` (which writes to XMP).
10. `export-date-trap` (LOW, `note`) — flags files with `ModifyDate` present but no `DateTimeOriginal`/`CreateDate`. Canonical cause: Lightroom / Photos export preset that stamped modify-time but dropped capture-time. Such files sort on the Immich timeline at export instant (months/years after capture). No auto-fix — surfaces in the per-file flags column so the user can re-export or delete.
13. `geotag-from-gpx` (HIGH) — when a `.gpx` track sits anywhere under the trip folder, matches each GPS-less media to the nearest track point within 5 min and writes XMP GPS. Requires a known offset for the image (EXIF `OffsetTimeOriginal`, a `±HH:MM` suffix on the XMP date, or `timezone:` in notes); skips files whose time can't be aligned to the UTC track. Loses the per-field dedup to `dji-gps-from-srt` (more specific) but wins over `trip-gps-anchor` (folder-wide constant).

12. `bloat-candidate` (LOW, `note`) — scores videos by `bitrate / (w·h·fps)` (bits-per-pixel-per-frame) and flags anything above the delivery-quality threshold (0.15 for H.264, 0.08 for HEVC). Stands down entirely for camera-native prefixes (`DJI_`, `GX`, `GH`, `GOPR`, `MAH`, `MVI_`, `C0`, `LRV_`, `PRO_`, `DSC_`, date-stamped `VID_YYYYMMDD` / `IMG_YYYYMMDD`), preserve extensions (`.insv`, `.insp`, `.lrv`, `.lrf`, `.mts`, `.dng`, `.braw`), preserve codecs (ProRes, DNxHR, CineForm, FFV1, RAW), Insta360 content (any extension, per user preference), and folders whose name contains `raw` / `source` / `edit` / `project`. Feeds Phase 2c's group-by-folder confirm UI — the rule itself never transcodes, just surfaces candidates.

13b. `makernote-present` (LOW, `note`) — flags files carrying a vendor `MakerNotes:*` block. Advisory only: immy's sidecar-only invariant means we don't rewrite the original EXIF IFD, so the `reason` line hands the user an `exiftool -overwrite_original -MakerNotes= <file>` command they can run themselves if privacy matters. Silent on files without MakerNotes (most phone captures).

14. `geocode-place` (HIGH, `write_notes`) — when notes front-matter has `location.name:` but no `location.coords:`, resolves the name to coords via Nominatim (OpenStreetMap) with a 5 s timeout and writes `[lat, lon]` back into the front-matter. Results cache at `~/.immy/places.yml` keyed on the name string, so a flaky uplink on the next run still reuses the previous answer. Silent skip on any network or parse failure (the offline Mac doesn't spam errors — the interactive coords prompt still works). Next apply pass lets `trip-gps-anchor` (HIGH) cascade the new coords to every GPS-less media in the folder.

11. `clock-drift-by-camera` (MEDIUM) — cross-camera group drift. Groups rows by `(Make, Model)`; when ≥2 groups each have ≥3 samples, picks the camera with the most GPS-tagged files as the reference (tie-breaks on group size), and proposes `DateTimeOriginal = original + delta` for every file in each off-group. Thresholds are conservative: ignores drift <5 min (sync noise) and >14 days (probably "different trip"). Findings share a `group` key so the MEDIUM prompter asks once per camera, not once per file. Stands in complement to `clock-drift` (folder-median): that one handles single-file outliers; this one handles "camera B was 3 h behind for the whole trip". `clock-drift` defers to this rule when it detects ≥2 camera groups.

**Batch prompting.** Findings with a non-empty `group` field collapse
into a single y/n prompt under the MEDIUM prompter (e.g. *"Sony ILCE-7M4
(80 files) is -3h00m vs Nikon Z50 (120 ref files) — apply to all?"*).
Per-file prompts remain the default when no group is set.

**Interactive pre-flight prompts.** Before rule evaluation, `immy audit`
(unless `--auto` or `--dry-run`) can ask the user two questions whose
answers become front-matter in the notes file and thereby drive HIGH
rules on the same run: (a) trip GPS anchor, when some media lack GPS
and `location.coords` is unset; (b) trip IANA timezone, when some media
dates are naive and `timezone:` is unset. Empty input skips; invalid
zone names are rejected with a clear message and don't write.

**Per-tier, per-field dedup.** Rules dedup within their confidence tier.
A HIGH rule claims `(path, xmp_field)` and later HIGH rules lose; a
MEDIUM rule with the same field is still surfaced because the user must
explicitly override HIGH. That's the whole point of the tier split —
`clock-drift` MEDIUM survives even when `trip-timezone` HIGH also wrote
`DateTimeOriginal`, so the user gets the chance to say "no, the camera
clock was wrong, apply the median instead". Within a tier, earlier-
registered wins (specific > general).

**Date authority.** `immy.dates.resolve(row)` returns a `DateAuthority`
= `(dt, source, raw)` where `source ∈ {exif, companion, filename, mtime}`
with a rank score. Lookup order: `XMP:DateTimeOriginal` (sidecar override
wins — that's how accepted clock-drift writes persist across audits),
`EXIF:DateTimeOriginal`, `QuickTime:CreateDate`, `EXIF:CreateDate`,
`EXIF:ModifyDate`, then companion `.SRT`, then filename pattern, then
`st_mtime`. `clock-drift` is the first consumer; other rules will adopt
it as the date contract grows.

**Two-pass apply.** Some rules depend on fields another rule writes in the
same audit (trip-timezone needs the date that dji-date-from-srt produces).
After applying HIGH findings, `immy` re-reads EXIF + merges adjacent `.xmp`
sidecars into each row, re-evaluates rules, and applies any new findings.
Capped at 3 passes; stops at a fixed point.

**YAML rules, later.** The declarative schema below is the planned public
surface for user-defined rules in a future iteration (likely 2a.6 or
later). Several shipped rules (insta360 pairing, trip-timezone's
compute-offset-at-date, the interactive coords prompt) are stateful
enough that "data-only YAML" would obscure more than it reveals — they'll
stay in Python. Simpler rules (has-sibling + missing-field → copy) will
migrate when it's cheap.

```yaml
# Target schema, not yet implemented
id: dji-gps-from-srt
confidence: high
priority: 10
match:
  has_sibling: "{stem}.SRT"
  missing: [GPSLatitude]
fix:
  parse: srt
  write:
    target: xmp
    fields: [GPSLatitude, GPSLongitude, GPSAltitude]
```

### Folder notes file (`README.md` / `TRIP.md` / `IMMY.md`)

Every trip folder gets **one** notes file at its root. **Visible in Finder,
editable in any text editor, written first by `immy` from the first audit's
decisions, freely extended by the user afterwards.** It's the file you open
in three years to remember where a trip was and why the clock was weird.

**Resolution order (read):** first file that exists with valid YAML
front-matter wins.

1. `TRIP.md`
2. `IMMY.md`
3. `README.md`

**Write policy:**
- No candidate exists → write `README.md` (most universal).
- `README.md` exists *without* front-matter → insert the front-matter block
  at the top, leave the existing body intact.
- One of the three already has front-matter → keep writing to whichever it is.

User can pin a project-wide preference in `~/.immy/config.yml`:
`notes_filename: IMMY.md`.

**Scaffold on first audit (as shipped):** when none of the three files
exist, `immy audit` creates `README.md` with detected identity (trip
name from folder, detected capture dates, detected cameras, detected
filename prefixes), a `location: { name: null, coords: null }` stub,
`timezone: null`, and a suggested `tags:` list built from
`Events/<folder>`, `Gear/Camera/<detected-camera>`, `Source/<prefix>`.
Editing any of these fields and re-running `immy audit --write` applies
the new values on the next pass.

**Schema (any of the three filenames):**

```markdown
---
trip: Mau-Lions-1
dates: [2026-03-05]
location:
  name: Casela Nature Parks, Mauritius
  coords: [-20.29627, 57.40794]
timezone: Indian/Mauritius
cameras:
  - Nikon Z50_2
tags:
  - Events/Mau-Lions-1
  - Gear/Camera/Nikon Z50_2
  - Source/Nikon
---

# Mau-Lions-1

Safari day at Casela. 66 frames, Z50 with the 70-300. Phone was off (park
rules), no iPhone GPS anchor — trip coordinates applied from front-matter
above. Saw the lions at the feeding window, giraffes blocked the north
track for twenty minutes.
```

**Split of responsibilities:**

| Field | notes file (human, visible) | `state.yml` (machine, `.audit/`) |
|---|---|---|
| Trip name | ✓ | ✓ (mirrored) |
| Location name / coords | ✓ (source of truth) | referenced |
| Timezone | ✓ (source of truth) | referenced |
| Camera list | ✓ | ✓ (mirrored) |
| Tags to apply | ✓ (user-editable) | ✓ (mirrored) |
| Free-form body (travel notes, shoot notes, project context) | ✓ | — |
| Per-rule decisions (clock offsets, pair groupings) | — | ✓ (source of truth) |
| Rule version history, JSONL log pointer | — | ✓ |
| Last audit status | — | ✓ |

**Contract**: `immy audit` reads the notes file's front-matter on start,
merges it into decision context (so location/timezone questions don't
re-ask), then re-writes the file preserving the user's body. Anything
below the front-matter is untouched. The front-matter is canonical for
trip-level data; `state.yml` is canonical for rule-level decisions.

**Nice side-effect** *(shipped)*: `immy promote` creates or updates an
Immich album named after the trip folder, sets its description from the
notes body (front-matter + `# Title` + scaffold-hint paragraph stripped),
and adds every media asset in the trip to it. Idempotent — re-running
promote leaves the album alone except for newly-indexed assets and a
description patch when the body changed. The notes body becomes
searchable prose in Immich without any separate sync.

### `state.yml` schema (as shipped)

Per-trip idempotency. Lives at `<trip>/.audit/state.yml`. Each entry is a
short patch-hash so a re-run with the same proposed fix is a no-op:

```yaml
# ~/Documents/Incoming/Mau-Lions-1/.audit/state.yml
applied:
  DSC_4182.JPG:
    trip-gps-anchor: 159e21de107b06ab
    trip-tags-from-notes: 8ae412fb92c1f0dd
    trip-timezone: 3df9a01bc7e20a14
    clock-drift: 6c1b80e43ae7fa9b     # MEDIUM, accepted 2a.2
  DSC_4381.JPG:
    trip-gps-anchor: 159e21de107b06ab
    ...
```

This shipped schema is intentionally flat — it answers "was this exact
patch applied already?" and nothing more. Both HIGH and MEDIUM
acceptances land in the same map (MEDIUM only after the user accepted
via the prompter or `--yes-medium`). A declined MEDIUM finding stays
out of `applied` and re-surfaces on the next audit — today there's no
`rules_skipped` opt-out list. Richer shape (a chosen reference camera,
a chosen clock offset for group drift, an explicit `rules_skipped`
list) is the target schema below:

```yaml
# Target for 2a.2+
applied: { ... }
decisions:
  clock-drift-vs-reference:
    fuji:
      offset: "+3:02:00"
      applied_at: 2026-04-18T10:32:14+01:00
      pairs: 14
rules_skipped: []                # user explicitly declined
last_audit_at: 2026-04-18T10:32:14+01:00
last_audit_status: clean         # clean | needs_review | error
```

### JSONL audit log

`<trip>/.audit/audit.jsonl` — append-only, one line per applied action.
Replayable, greppable. Unix-timestamp `ts` keeps the format simple:

```json
{"ts":1776450709.642,"event":"applied","rule":"trip-gps-anchor","file":"DSC_4381.JPG","action":"write_xmp","patch":{"GPSLatitude":"-20.296270","GPSLatitudeRef":"S","GPSLongitude":"57.407940","GPSLongitudeRef":"E"},"pair_with":null}
{"ts":1776450710.104,"event":"applied","rule":"trip-tags-from-notes","file":"DSC_4381.JPG","action":"write_xmp","patch":{"HierarchicalSubject":["Events/Mau-Lions-1","Gear/Camera/Nikon Z50_2","Source/Nikon"],"Subject":["Mau-Lions-1","Nikon","Nikon Z50_2"]},"pair_with":null}
```

Only `event: applied` entries are written today. Future iterations will
add `event: prompt`, `event: skipped`, `event: contradiction`.

### XMP tag write contract

Tags are hierarchical and live in XMP `lr:hierarchicalSubject` (Lightroom-style,
widely compatible) + `dc:subject` (flat fallback for tools that don't read
hierarchical).

```xml
<lr:hierarchicalSubject>
  <rdf:Bag>
    <rdf:li>Events|Iceland-Volcano-25</rdf:li>
    <rdf:li>Gear|Camera|Fuji X100V</rdf:li>
    <rdf:li>Source|Fuji</rdf:li>
  </rdf:Bag>
</lr:hierarchicalSubject>
```

Immich's sidecar import reads these on scan; the flat-subject fallback means
digiKam / Lightroom / Photo Mechanic see the same tags without us having to
teach them about the hierarchy separator.

Writing order per file (as shipped):
1. Engine computes the final set of `(field, value)` pairs to write for one
   finding.
2. One `exiftool -overwrite_original -XMP:<tag>=<value> ... <sidecar>` call
   per finding. Sidecar path is Adobe standard: `basename.xmp` (e.g.
   `DSC_4182.xmp` for `DSC_4182.JPG`). List-valued fields are emitted as
   repeated `-XMP:<tag>=<value>` assignments — first `=` clears the list,
   each subsequent `=` appends an entry, so re-applying the same list
   overwrites cleanly and is idempotent.
3. State is updated with the patch-hash before the next finding runs. The
   CLI's two-pass apply re-reads EXIF + sidecars between passes.

**`write_notes` action.** Used by `tag-suggest-missing`. The finding's
`path` is the notes file (`TRIP.md`/`IMMY.md`/`README.md`) and the
`patch` carries a `{"add_tags": [...]}` list. The CLI merges the list
into the front-matter `tags:` (unique, order-preserving via
`notes.update_frontmatter`) and writes the file. State records the
patch-hash on the notes path's relative string so a repeat proposal is
a no-op. After a `write_notes` apply, the two-pass loop re-reads EXIF +
re-evaluates, which lets `trip-tags-from-notes` see the new tags and
cascade them into each media's XMP sidecar without a separate trigger.

**Why `basename.xmp` and not `basename.ext.xmp`:** Adobe/Bridge convention.
Stream-pairs that share a stem (Live Photo HEIC+MOV) collapse to one
sidecar, which is correct — they share capture metadata. Cross-type
collisions inside a single folder are rare in practice; a future
iteration can add a fallback to `basename.ext.xmp` if they ever show up.

### Sidecar read merge

`exiftool` does *not* auto-pair a media file with its adjacent `.xmp`
sidecar on read. `immy` does that itself: `read_folder` reads both media
and any `.xmp` siblings in one exiftool batch, then merges each sidecar's
`XMP:*` tags into the media row (sidecar keys only fill holes — the
media's own EXIF tags win where both are present). This is what lets a
pass-N rule see fields written by a pass-N–1 rule in the same audit.

### `immy` CLI surface (as shipped)

```
immy audit   <trip-folder>                     # read-only: print per-file table + pending/applied counts
immy audit   --write <trip-folder>             # apply HIGH findings; interactive for LOW + MEDIUM
immy audit   --write --yes-medium <trip-folder># also auto-accept every MEDIUM finding (no per-prompt)
immy audit   --auto <trip-folder>              # non-interactive: no LOW coords prompt, no MEDIUM prompt
immy audit   --dry-run <trip-folder>           # with --write: report but don't modify
immy audit   --verbose <trip-folder>           # per-file EXIF dump
immy promote <trip-folder>                     # rsync to originals_root + scan + stack Insta360 pairs
immy push    <trip-folder>                     # alias of promote
immy pub     <trip-folder>                     # alias of promote
immy promote --dry-run <trip-folder>           # report plan; no rsync, no API calls
immy promote --force <trip-folder>             # promote even if HIGH findings are still pending
immy promote --config <path> <trip-folder>     # override ~/.immy/config.yml for one run
```

Combining flags is fine: `--write --auto` is a pure-HIGH automated pass
(MEDIUM stays pending for a later interactive run), while
`--write --auto --yes-medium` is the watcher recipe — no prompts, HIGH +
MEDIUM both apply.

**MEDIUM prompter flow.** After the HIGH apply loop converges, if any
MEDIUM findings are pending `immy` re-reads EXIF, re-evaluates, and
surfaces each remaining MEDIUM finding with its `reason` line (e.g.
`+4.0d off folder median (source=exif, this=2026-04-05 12:00:00,
median=2026-04-01 10:07:30)`) and the proposed `DateTimeOriginal` value.
User answers `y`/`n` per finding. Accepted findings go through the same
apply loop (state + JSONL log same as HIGH). `--yes-medium` short-
circuits the prompt and accepts them all.

**`--yes-high` is intentionally not shipped yet.** Under `--write` today,
HIGH findings apply unconditionally — there's no prompt to opt out of.
The flag name is reserved for 2a.6 (watcher) when HIGH findings gain a
confirm step outside the declarative coords/tz/etc. prompts.

Target for later iterations:

```
immy status  <trip-folder>                # last audit summary + pending issues
immy rules   list [--explain <rule-id>]   # catalogue
```

Exit codes today: `0` on any completed audit (prompt-declined included),
non-zero on exiftool/file errors. Rule-contradiction and "pending
MEDIUM/LOW" exit codes are still TODO — the 2a.2 MEDIUM prompter landed
without differentiated exit codes so that CI-style non-interactive runs
under `--auto` don't fail just because a MEDIUM finding is pending
review. Distinguishing pending-MEDIUM from clean will land with 2a.6
(watcher mode needs an exit code to drive the `NEEDS_REVIEW.md`
generation).

### `immy promote` — rsync + scan + stack + album

Shipped in 2a.4; album sync added post-2c. Same engine as `audit` (so
the same notes, state, and audit-log file stay authoritative), wrapped
in four phases:

1. **Guard rail.** Re-evaluate rules; if any HIGH finding is pending,
   refuse with exit 1. Override with `--force`. This stops "promote what
   you forgot to audit" mistakes — the whole contract is that
   `originals/` only receives folders whose metadata has already been
   fixed on disk.
2. **Rsync.** `rsync -av --itemize-changes <folder>/ <originals_root>/<folder>/`.
   `.audit/` is excluded (machine state stays on the Mac), along with
   `.DS_Store`, Spotlight dirs, and similar OS noise. XMP sidecars and
   the folder notes file travel with the media. Destination parent is
   auto-created for local targets; remote `user@host:/path` is the
   caller's setup problem.
3. **Immich handoff.** `POST /api/libraries/{library_id}/scan` kicks the
   scan. For each `.insv` ↔ `.lrv` pair that the
   `insta360-pair-by-ts-serial` rule surfaced, `immy` polls
   `POST /api/search/metadata?originalFileName=...` until both assets
   are indexed (6 tries × 2 s), then `POST /api/stacks` with the
   `.lrv` asset ID first (Immich 2.x treats the first element of
   `assetIds` as the stack primary). Missing IDs → logged, not fatal.
4. **Album sync.** `GET /api/albums` → find an album whose `albumName`
   matches the trip folder name, else `POST /api/albums` with the
   notes body as description. Every local media file in the trip gets
   its asset ID looked up (first file polls until it's indexed — scan
   is async; subsequent files use one-shot lookups so large trips don't
   spend minutes per file) and bulk-added via `PUT /api/albums/{id}/assets`
   (the endpoint reports already-present assets as duplicates, so re-runs
   are safe). Description gets `PATCH`ed only when the notes body
   changed. Errors are caught and surface as `album error …` without
   failing the promote — the photos are on disk and in Immich regardless.

**Config** lives at `~/.immy/config.yml` (override with `--config` or
`$IMMY_CONFIG`). Example:

```yaml
originals_root: /mnt/incoming/originals-test   # can be a remote rsync target
immich:
  url: ${IMMICH_URL}
  api_key: <key>
  library_id: <uuid>                            # the External Library ID
notes_filename: TRIP.md                         # optional
```

`immich:` is optional — when missing, promote is rsync-only (useful for
local staging trees before the real External Library exists).
`originals_root` is required; promote exits 2 with a clear message
otherwise. The `--dry-run` flag skips both rsync writes and every API
call; the CLI reports what *would* have happened (rsync itemised diff,
"would stack X ↔ Y" per pair).

The JSONL audit log on the source folder gets one extra event per run:
`{"event": "promoted", "target": "...", "pair_count": N}` so a trip's
full story (audit decisions → promote) is in one file.

### `immy bloat` — Phase 2c CLI

CLI-first version of the bloat-detector + batch-transcode workflow. Reuses
the `bloat-candidate` rule's preserve allowlist and bpp scoring (source of
truth lives in `immy/rules/bloat_candidate.py`); adds candidate records
with estimated post-transcode size, grouping, and the ffmpeg path.

```
immy bloat list <folder>               # scan + group-by-folder summary
immy bloat transcode <folder>          # per-group y/n → .optimized.ext sidecar
immy bloat transcode <folder> --apply  # after verify, atomic-replace originals
immy bloat transcode <folder> --dry-run
immy bloat transcode <folder> --yes    # non-interactive (still groups output)
```

**Target bitrate.** `target = w * h * fps * 0.05` (HEVC delivery bits-per-
pixel-per-frame), rounded to nearest 0.5 Mbps. That's half the
`bloat-candidate` rule's threshold (0.08) so a second scan after transcode
comes back clean.

**Savings gate.** Candidates where `(current - estimated) / current <
0.20` are dropped before prompting — not worth CPU + quality hit.

**Per-folder confirm (never auto).** The scan groups candidates by parent
directory, and the CLI prompts once per group with the file count and
total GB saved. Matches `feedback_transcode_confirm`: the user's call,
never the machine's.

**ffmpeg invocation.**
```
ffmpeg -y -hide_banner -loglevel warning \
  -i <src> -c:v hevc_videotoolbox -tag:v hvc1 \
  -b:v <target_bitrate> -c:a copy <stem>.optimized.<ext>.part
```
Writes to `.part` first, then renames to `.optimized.<ext>` after a
verify pass: `ffprobe` duration match ±0.5 s and stream count match.

**Non-destructive by default.** Transcodes land next to the source as
`<stem>.optimized.<ext>`. `--apply` (separate flag) does the swap:
`<src>` → `<src>.original`, `<src>.optimized.<ext>` → `<src>`. A
`<src>.transcode.json` receipt lands alongside with:

```json
{
  "pre_sha256": "...",
  "pre_size": 1073741824,
  "post_size": 268435456,
  "pre_bitrate": 15000000,
  "post_bitrate_target": 3000000,
  "codec_before": "h264",
  "codec_after": "hevc",
  "width": 1920, "height": 1080, "fps": 30,
  "original_name": "edit.mp4.original"
}
```

The `.original` file is **not** auto-deleted — the user reviews visually
and removes it when satisfied. No catalog DB involvement yet (that lands
in the sidecar schema described below); the receipt JSON is the only
persistence layer for now.

**Preserve allowlist.** Identical to the `bloat-candidate` rule — camera-
native prefixes (`DJI_`, `GX`, `GH`, `GOPR`, `MAH`, `MVI_`, `C0`, `LRV_`,
`PRO_`, `DSC_`, date-stamped `VID_YYYYMMDD`/`IMG_YYYYMMDD`), preserve
extensions (`.insv`, `.insp`, `.lrv`, `.lrf`, `.mts`, `.dng`, `.braw`),
preserve codecs (ProRes / DNxHR / CineForm / FFV1 / RAW), Insta360 content
(any extension), and folders whose segment contains `raw` / `source` /
`edit` / `project` / `insta360`.

### `immy process` — Phase Y.1 direct-to-DB insert

Writes `asset` + `asset_exif` rows directly into Immich's Postgres over the
tailnet, so the library scan becomes a no-op for Mac-handled trips. See
[IMMICH-INGEST.md](IMMICH-INGEST.md) §1 + §8 for the exact schema; the
rest of Phase Y builds derivatives on top of this.

```
immy process <folder>            # connect, INSERT asset+exif, drop marker
immy process <folder> --dry-run  # report would-insert; no DB writes
```

**Config.** Reads a `pg:` block from `~/.immy/config.yml` (host/port/user/
password/database) plus the existing `immich.library_id`. Owner UUID and
container-path prefix come from the `library` row at runtime — no
duplication with what Immich already knows.

**Checksum = `sha1("path:" + container_path)`**, 20 raw bytes, stored with
`checksumAlgorithm='sha1-path'`. Matches the value Immich's own scanner
computes for external-library files, so `ON CONFLICT DO NOTHING` on
`(ownerId, libraryId, checksum)` makes the insert idempotent — scan and
process cannot double-insert the same path.

**Marker file.** On success, `.audit/y_processed.yml` lists every inserted
asset (uuid + container path + `new: true|false`). `immy promote` checks
for this marker; when present, it skips the `POST /api/libraries/:id/scan`
call — the rows are already there. Marker path is stable
(`immy.process.marker_path(folder)`), so later Y phases can extend it with
derivative-path records without breaking the skip logic.

**Y.1 scope = metadata only.** No thumbnails, no smart_search, no faces —
those are Y.2/Y.3/Y.4. A Y.1-processed trip shows up in the Immich UI with
placeholder thumbs and full EXIF detail; the library scan still won't try
to re-process it.

### `immy process --with-derivatives` — Phase Y.2

Adds thumbnail (250 px WebP q80) + preview (1440 px JPEG q80 progressive)
generation via `pyvips` (libvips), the same library Immich's `sharp`-based
worker uses. Output matches §3 bucketing:

```
<trip>/.audit/derivatives/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_thumbnail.webp
<trip>/.audit/derivatives/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_preview.jpeg
```

Staging is deliberately local — compute (Mac, Metal) and upload (NAS,
bandwidth-bound) are split so a flaky uplink can resume the rsync without
re-encoding. Only newly-inserted IMAGE assets get derivatives; checksum
conflicts (already indexed) and VIDEO rows (Y.5) are skipped.

**Config.** Requires a `media:` block alongside `pg:`:

```yaml
media:
  host_root: ${SHARED_LIBRARY}               # rsync destination (NAS-side)
  container_root: /data                      # IMMICH_MEDIA_LOCATION in the container
```

`host_root` can be a remote rsync target (`user@host:/path`) when the Mac
isn't SMB-mounting the share. `container_root` is the same tree as the
server sees — it becomes the prefix in every `asset_file.path` we INSERT.

**Marker extension.** Each asset entry in `.audit/y_processed.yml` gains
a `derivatives:` list recording `kind` / `relative_path` / `is_progressive`
/ `is_transparent`. `immy promote` reads the marker and, when the scan-skip
branch fires, rsyncs `.audit/derivatives/` → `media.host_root` and
`INSERT … ON CONFLICT (assetId, type, isEdited) DO UPDATE` one `asset_file`
row per derivative. The rsync-then-DB order matters — a `preview.jpeg`
row with no file on disk 404s the detail view.

**Skipped when `media:` is absent.** `immy process --no-derivatives`
disables staging explicitly; without `media:` the process command warns
and continues Y.1-only.

### Insta360 `.insv` handling

Immich v2.7 has no native 360 player and can't use a "preview file" out of
the box. The `.insv` original is an H.264 MP4 with a proprietary dual-fisheye
layout that looks like two circles side-by-side when any normal player opens
it. The `.lrv` sibling is an already-stitched low-res proxy, usually
watchable in a flat 2:1 frame.

So the pipeline is:

1. **Pair by timestamp+serial** (`immy` rule `insta360-pair-by-ts-serial`).
   `.insv` and `.lrv` don't share filename stems — they share the filename
   timestamp block (`20260226_072504`) and camera serial (`_00_` or `_01_`).
2. **Stamp date + GPS on both** from the most reliable source (filename
   timestamp + companion `.lrv` GPS if present). Writes go to each file's
   XMP sidecar.
3. **Promote both** into `originals/<Trip>/insta360/`. Immich sees and
   indexes both as separate assets.
4. **After the scan completes**, `immy` calls Immich's `POST /api/stacks`
   with both asset IDs, marking the **`.lrv` as primary**. The timeline
   shows one tile per shot; clicking opens the `.lrv` preview; the detail
   view lets you download the `.insv` for editing in Insta360 Studio.

Optional later (Phase 2b, not 2a):
- **Stitched equirectangular MP4 proxy** via Insta360 CLI or `ffmpeg` with
  a dual-fisheye → equirect filter. When it exists, it joins the stack
  and takes over as the primary (prettier than `.lrv`'s reframed crop).
- **Equirect-aware viewer** (Pannellum / Marzipano) is a sidecar route, not
  an Immich modification. Only worth building if we shoot enough 360 that
  flat 2:1 previews feel limiting.

**Editing flow** stays unchanged: Mac mounts `${SHARED_ORIGINALS}`
over SMB → open `.insv` in Insta360 Studio → re-export → drop result into
`~/Documents/Incoming/<Trip>/insta360/` as a new asset.

### What `immy` does NOT do

- **Does not touch originals that are already in `${SHARED_ORIGINALS}`.**
  Post-ingest fixes are the Phase 5 gap-fill UI's job.
- **Does not decide AI stuff** (tags inferred from CLIP captions, faces, Whisper).
  Those arrive via the post-ingest workers described below.
- **Does not own the Immich API client for enrichment** — only the narrow
  "create external library scan" call. Enrichment workers talk to Immich
  directly.

## Why a separate database on Immich's Postgres

Considered and rejected:

- **SQLite on the Mac.** Syno-side fallback workers (stock Immich CPU
  ML, watcher, non-Metal workers) can't reach it. Mac sleep makes the
  queue unreachable. Non-starter.
- **A second Postgres container.** Pure ops burden — another instance
  to back up, upgrade, tune — with no benefit when there's already a
  well-tuned PG on NVMe with chattr +C.
- **A new schema inside the `immich` database.** Couples sidecar to
  Immich's migrations. An Immich upgrade that resets or recreates the
  DB stomps the queue.

The separate-database option keeps a single Postgres process but
isolates the sidecar's schema, backups (`pg_dump sidecar`), and
migrations. Mac → Syno connectivity is already solved by Tailscale
(Phase 0 is Tailscale-first).

## Schema

Five tables. Everything keys off `asset_checksum` so the sidecar's view
of an asset lines up with Immich's own identity.

```sql
-- the queue itself
CREATE TABLE jobs (
  id              BIGSERIAL PRIMARY KEY,
  asset_checksum  TEXT NOT NULL,
  worker_name     TEXT NOT NULL,
  worker_version  TEXT NOT NULL,
  status          TEXT NOT NULL,        -- pending|running|done|failed|skipped
  priority        SMALLINT DEFAULT 100, -- lower runs sooner
  payload         JSONB,                -- worker-specific input
  result          JSONB,                -- worker-specific output
  error           TEXT,
  attempts        INT DEFAULT 0,
  locked_by       TEXT,                 -- hostname:pid
  locked_until    TIMESTAMPTZ,          -- lease expiry
  created_at      TIMESTAMPTZ DEFAULT now(),
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  UNIQUE (asset_checksum, worker_name, worker_version)
);
CREATE INDEX jobs_claim_idx
  ON jobs (worker_name, worker_version, priority, created_at)
  WHERE status = 'pending';

-- cheap identity without full-file hashing (see ARCHITECTURE.md §ingest)
CREATE TABLE asset_fingerprints (
  asset_checksum   TEXT PRIMARY KEY,
  tail_sha256      TEXT NOT NULL,         -- sha256 of last 1 MB
  size_bytes       BIGINT NOT NULL,
  mtime            TIMESTAMPTZ NOT NULL,
  first_seen_path  TEXT NOT NULL,
  first_seen_at    TIMESTAMPTZ DEFAULT now()
);

-- bloat re-encode audit trail — what we replaced and with what
CREATE TABLE transcodes (
  id                    BIGSERIAL PRIMARY KEY,
  asset_checksum        TEXT NOT NULL,
  pre_transcode_sha256  TEXT NOT NULL,
  pre_size_bytes        BIGINT NOT NULL,
  post_size_bytes       BIGINT,
  codec_before          TEXT, codec_after    TEXT,
  bitrate_before        BIGINT, bitrate_after BIGINT,
  ffmpeg_cmd            TEXT,
  ffmpeg_log_path       TEXT,
  confirmed_by          TEXT NOT NULL,       -- web-UI session id
  confirmed_at          TIMESTAMPTZ NOT NULL,
  applied_at            TIMESTAMPTZ
);

-- where proxies / posters / transcripts landed on tier-0
CREATE TABLE artifacts (
  id              BIGSERIAL PRIMARY KEY,
  asset_checksum  TEXT NOT NULL,
  kind            TEXT NOT NULL,    -- poster|proxy_1080p|transcript_srt|preview_embedded
  path            TEXT NOT NULL,
  bytes           BIGINT NOT NULL,
  created_at      TIMESTAMPTZ DEFAULT now(),
  UNIQUE (asset_checksum, kind)
);

-- mount adapter health so the scanner doesn't hang on dead shares
CREATE TABLE mount_health (
  name        TEXT PRIMARY KEY,     -- 'archive-2024', 'icloud', 'desktop-scratch'
  status      TEXT NOT NULL,        -- online|offline|degraded
  last_check  TIMESTAMPTZ DEFAULT now(),
  detail      JSONB
);
```

Event-cluster draft state, gap-fill sessions, and near-dup reports get
their own tables when Phases 4–7 land. Don't build them now.

## Worker-harness contract

A worker is a class in `sidecar.workers.<name>`. The harness does the
claim / lease / retry / shutdown dance; workers stay dumb.

```python
class Worker(Protocol):
    name: str                # e.g. "bloat_detector"
    version: str             # "1.0.0" — bump invalidates prior results
    lease_seconds: int       # 300 typical; 3600 for whisper/transcode

    def process(self, job: Job, heartbeat: Callable[[], None]) -> dict:
        """Idempotent. Return result dict. Raise to fail."""
```

Claim is a single statement. `FOR UPDATE SKIP LOCKED` lets N workers of
the same type run side-by-side with zero coordination:

```sql
UPDATE jobs
SET status='running', locked_by=$host, locked_until=now()+$lease,
    started_at=now(), attempts=attempts+1
WHERE id = (
  SELECT id FROM jobs
  WHERE status='pending'
    AND worker_name=$name AND worker_version=$version
    AND (locked_until IS NULL OR locked_until < now())
  ORDER BY priority, created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

Heartbeat extends `locked_until` for long jobs. SIGTERM releases the
lock cleanly. Crash or hung process → lease expires → another worker
picks it up. Failed attempts increment `attempts`; `status='failed'`
only after N tries with exponential backoff.

Enqueue is an upsert on the `(checksum, worker, version)` unique index:
re-enqueuing a `done` job is a no-op; bumping `worker_version` creates
a fresh row and re-runs.

## Process layout

One repo, one package, many `python -m sidecar.worker <name>` entry
points. Split by host along the Metal line:

| Process | Host | Purpose |
|---|---|---|
| `sidecar-watcher` | Syno | Inbox poller. Enqueues `preview_extractor` + `bloat_detector` |
| `sidecar-worker preview_extractor` | Syno | exiftool header read, embedded JPEG / LRV harvest |
| `sidecar-worker bloat_detector` | Syno | bits/pixel/frame scoring — detection only, no transcode |
| `sidecar-worker transcoder` | Mac | `hevc_videotoolbox`, only after per-folder confirm (see [feedback_transcode_confirm](../)) |
| `sidecar-worker whisper` | Mac | `whisper.cpp` Metal → `.srt` sidecar |
| `sidecar-worker captioner` | Mac | moondream2 / BLIP → description prefix |
| `sidecar-worker clip_sync`, `face_sync` | Mac | Push embeddings/results to Immich via REST |
| `sidecar-web` | Syno | FastAPI: `/gap` + `/transcode` confirm UIs |

Syno processes ship as a second docker-compose project alongside
`${COMPOSE_PROJECT}`. Mac processes start under `launchd` so they survive reboots
and respect sleep/wake. All read the same `DATABASE_URL`.

## What the sidecar does NOT own

- Asset rows, albums, tags, descriptions, faces — Immich owns those,
  sidecar updates them via REST.
- CLIP / face embeddings — Immich's Postgres + pgvector.
- Originals — read-only mounts, one-way. The only writer is the
  transcoder, and only after confirm + atomic replace.
- A web UI that duplicates Immich. The sidecar's only UI surfaces are
  task-specific (gap-fill, transcode confirm).

Keeping the blast radius small is the whole point: if the sidecar
disappears tomorrow, Immich still works.
