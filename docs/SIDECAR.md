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

### Rule YAML schema

Rules are data, not code. Engine is generic (~300 LoC Python); new rules
are YAML entries under `immy/rules/*.yml`.

```yaml
# immy/rules/dji-gps-from-srt.yml
id: dji-gps-from-srt
description: DJI clips carry GPS in the SRT sidecar, not the MP4 header.
confidence: high                # high | medium | low
priority: 10                    # lower runs first
match:
  ext: [.MP4, .MOV]
  filename_prefix: "DJI_"
  has_sibling: "{stem}.SRT"
  missing: [GPSLatitude]
fix:
  parse: srt                    # built-in parser name
  write:
    - target: xmp               # XMP sidecar next to the file
      fields: [GPSLatitude, GPSLongitude, GPSAltitude]
    - target: exif              # also bake into container if safe
      fields: [GPSLatitude, GPSLongitude, GPSAltitude]
```

```yaml
# immy/rules/clock-drift-vs-reference.yml
id: clock-drift-vs-reference
description: Shift a camera's clock to match a trusted reference.
confidence: medium
priority: 50
match:
  camera_median_offset_vs_reference: ">10m"
reference_priority:              # first that exists in the trip wins
  - iphone
  - gopro
  - camera_with_most_files
fix:
  shift_all_dates: "{offset}"    # engine computes {offset} from the match
  write:
    - target: [exif, xmp]
      fields: [DateTimeOriginal, CreateDate, ModifyDate]
prompt: |
  {camera} median is {offset} off vs {reference} on {pairs} overlapping
  hour windows. Apply this shift to all {count} files?
```

```yaml
# immy/rules/event-tag-from-folder.yml
id: event-tag-from-folder
description: Folder name becomes the event tag.
confidence: high
priority: 90
match:
  folder_depth: 1                # immediate child of Incoming/
fix:
  add_tag: "Events/{folder_name}"
  write:
    - target: xmp
      fields: [HierarchicalSubject]
```

Rule fields:
- `id` — unique, kebab-case, also the key in `state.yml`.
- `confidence` — decides interaction: `high` auto-applies, `medium` prompts
  y/n with `prompt`, `low` asks open-ended.
- `priority` — ordering when multiple rules match; lower runs first. Two
  rules writing the same field must have different priorities or the engine
  fails the audit (no silent "last-rule-wins").
- `match` — declarative filter. Built-ins: `ext`, `filename_prefix`,
  `filename_regex`, `has_sibling`, `missing`, `present`, `folder_depth`,
  `folder_name_regex`, `make`, `model`, `codec`, `camera_median_offset_vs_reference`.
- `fix` — one of: `parse`, `copy_from_sibling`, `shift_all_dates`, `add_tag`,
  `set`, plus `write` targets (`exif`, `xmp`, or both).
- `prompt` — Jinja template, rendered with match context. Required for
  `confidence: medium | low`.

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

**Nice side-effect**: on `immy promote`, the body of the notes file can
become the Immich album description (via `PUT /api/albums/:id`) — search
"safari at Casela" and the album with that prose comes up.

### `state.yml` schema

Per-trip decision memory. Lives at `<trip>/.audit/state.yml`. Answers persist
so re-runs don't re-ask.

```yaml
# ~/Documents/Incoming/Iceland-Volcano-25/.audit/state.yml
trip_name: Iceland-Volcano-25
schema_version: 1
reference_camera: iphone
timezone: Atlantic/Reykjavik
decisions:
  clock-drift-vs-reference:
    fuji:
      offset: "+3:02:00"
      applied_at: 2026-04-18T10:32:14+01:00
      pairs: 14
  event-tag-from-folder:
    tag: "Events/Iceland-Volcano-25"
    applied_at: 2026-04-18T10:32:14+01:00
rules_skipped: []                # rules the user explicitly declined
last_audit_at: 2026-04-18T10:32:14+01:00
last_audit_status: clean         # clean | needs_review | error
```

### JSONL audit log

`<trip>/.audit/log.jsonl` — append-only, one line per action. Replayable,
greppable.

```json
{"ts":"2026-04-18T10:32:10Z","rule":"dji-gps-from-srt","file":"dji/DJI_0042.MP4","action":"apply","fields":["GPSLatitude","GPSLongitude"],"confidence":"high"}
{"ts":"2026-04-18T10:32:11Z","rule":"clock-drift-vs-reference","camera":"fuji","action":"prompt","confidence":"medium"}
{"ts":"2026-04-18T10:32:14Z","rule":"clock-drift-vs-reference","camera":"fuji","action":"apply","offset":"+3:02:00","answer":"y"}
```

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

Writing order per file:
1. Engine computes the final set of `(field, value)` pairs to write.
2. One `exiftool -overwrite_original -tagsFromFile @ -all:all` call to stamp
   the file and emit the `.xmp` sibling — single subprocess per file, not one
   per field.
3. Re-read, diff, assert success before logging the action.

### `immy` CLI surface

```
immy audit   <trip-folder>        # default: interactive, prompts MEDIUM/LOW
immy audit   --yes-high  <path>   # auto-apply HIGH only, skip and report others
immy audit   --yes-medium <path>  # auto-apply HIGH + MEDIUM using cached answers
immy audit   --dry-run  <path>    # nothing touched, just prints planned actions
immy promote <trip-folder>        # rsync to NAS + trigger Immich scan
immy promote --dry-run <path>     # print rsync cmd + API call, do neither
immy status  <trip-folder>        # show last audit summary + pending issues
immy rules   list                 # catalogue; `--explain <rule-id>` prints the YAML
```

Exit codes: `0` clean / no pending issues. `1` user aborted. `2` pending
MEDIUM/LOW. `3` rule contradiction or configuration error. Watcher mode uses
`0`-only as the auto-promote signal.

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

**Editing flow** stays unchanged: Mac mounts `/volume1/faeton-immi/originals/`
over SMB → open `.insv` in Insta360 Studio → re-export → drop result into
`~/Documents/Incoming/<Trip>/insta360/` as a new asset.

### What `immy` does NOT do

- **Does not touch originals that are already in `/volume1/faeton-immi/originals/`.**
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
`fnim`. Mac processes start under `launchd` so they survive reboots
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
