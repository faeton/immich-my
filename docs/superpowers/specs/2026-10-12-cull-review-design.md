# cull — a review tool for any import candidates

Date: 2026-10-12. Status: design approved in chat, spec under review.

## Why

The teslacam viewer worked well for the 2023 dashcam dump: look, keep/drop,
render, promote. The rest of the gdrive inventory needs the same "look before
it goes in" step, but it is not dashcam footage: DJI masters (unsorted1/Cine —
Mavic 3 Cine MP4+DNG+JPG+SRT+LRF; unsorted1/audiq7 — Mavic Pro MOV+JPG;
"Incoming - Drone+La Manga" — 78 missing masters), loose phone videos
(vd-1.mov), and whatever the next dump brings.

What the user asked for:

- **Cull only.** Each file is kept or dropped, with optional tags and a note.
  Kept originals go in untouched — no trimming, no re-encoding, no renders.
- **The tool stages trips.** In the viewer a selection is given a trip folder
  name (suggested from dates and places); a `stage` command hardlinks the kept
  files and their companions into `/mnt/tank/media/trips/<trip>/`. From there
  the usual `immy audit` → `immy srt` → `immy promote` takes over.
- **Reachable from the laptop over Tailscale**, not only via an SSH tunnel.

Success: Cine, audiq7, La Manga and vd-1 are reviewed in a browser on the
laptop, the keepers land in trip folders that `immy audit` accepts as-is, and
nothing in the sources is modified or deleted by the tool.

## Approach

A separate tool on teslacam's plumbing (option A of three; a cull mode bolted
onto teslacam, and one viewer with source plugins, were rejected — the first
leaves Tesla concepts in a drone tool, the second puts a just-shipped viewer
at risk). Culling hundreds of single clips is a different job from trimming a
few multi-camera events, so the UI is new; the plumbing is the same.

## 1. Layout

New uv project `immich-my/cull/`:

- `cull.core` — moved out of `teslacam/viewer/server.py`: the Range-aware
  file sender, path resolution that rejects escapes, atomic JSON writes, the
  quiet threading server, and host resolution (below). No teslacam concepts.
- `cull.items`, `cull.scan`, `cull.stage`, `cull.viewer` (server + one-file
  `index.html`), `cull.cli`.

teslacam takes `cull` as a path dependency (`[tool.uv.sources] cull =
{ path = "../cull", editable = true }`) and its viewer server imports the core
pieces instead of carrying its own copies. teslacam's routes, API, UI and
decision files do not change; its full test suite must pass unchanged.

CLI:

```sh
uv run cull scan   --src DIR [--src DIR ...] --work DIR [--immich] [--no-proxies]
uv run cull review --work DIR [--host 127.0.0.1|tailscale|ADDR] [--port 8778]
uv run cull stage  --work DIR --trips /mnt/tank/media/trips [--yes]
uv run cull report --work DIR [--dropped|--kept|--unreviewed]
```

### Host binding

`--host` defaults to `127.0.0.1`. `--host tailscale` binds to this machine's
Tailscale address, found from the interfaces (an IPv4 address in
100.64.0.0/10, e.g. `tailscale0` 100.115.236.50 on n5 — the host has no
`tailscale` CLI, so the CLI is not used); no such address → exit with an
error, never fall back to 0.0.0.0. Any other value is bound as given. The
printed URL uses the bound address. `teslacam review` gains the same flag
through the core.

## 2. Scan → items

`cull scan` walks each `--src` and builds review items. Sources are read only.

**Grouping.** An item is one master plus its companions: files in the same
directory with the same stem (case-insensitive). Masters: video
(`.mp4 .mov .m4v .avi .mts`) and stills (`.jpg .jpeg .heic .png .dng` and
other raw). Companions: `.srt .lrf .thm .xmp .aae`, matched by stem, plus a
`<name>.<ext>.xmp` sidecar of the master. A DNG+JPG pair with the
same stem is one item whose master is the DNG and whose companion is the JPG
(both are staged). An `.lrf`/`.srt` with no master is reported as an orphan
and not an item. AppleDouble `._*` files and `.DS_Store` are ignored.

**Item id** = `<src name>/<path of the master relative to its --src>`, where
the src name is the src directory's basename (`-2`, `-3` appended in `--src`
order when two srcs share a basename). Ids are stable across re-scans as long
as the sources do not move.

**Per item** (cached by path + size + mtime, so a re-scan only probes new or
changed files):

- kind (video/photo), size, duration, resolution, codec (ffprobe; exiftool for
  stills).
- capture time, approximate: the first DJI SRT timestamp (local wall time),
  else EXIF `DateTimeOriginal`, else QuickTime `CreationDate`, else
  `CreateDate` read as UTC, else file mtime (flagged `time-from-mtime`). It is
  used only for sorting, grouping and suggesting names; the real capture time
  is immy audit's job.
- GPS: first valid fix in the SRT, else EXIF GPS. Place: reverse-geocoded
  city through a Nominatim cache in `_cull/geocache.json` (1 req/s, offline
  works from cache; `--no-geocode` skips).
- **in Immich** (only with `--immich`; read-only queries against the Immich
  DB from `config.host.yml`): a video matches an asset with the same
  `localDateTime` within 5 s and duration within 1 s; a still matches on file
  size plus `localDateTime` within 2 s. Immich external-library checksums are
  path hashes, so content hashes are not used. A match shows the asset's
  original path.

**Playback.** The browser plays H.264 only. Order: the item's `.lrf` if
present; else the master itself when it is H.264 ≤ 1080p in MP4/MOV; else a
generated 480p H.264 proxy in `_cull/proxies/` (made during scan with a
progress line; `--no-proxies` defers it, and the viewer then shows the
thumbnail with "no proxy"). Stills: JPEG/PNG served directly; DNG/raw/HEIC →
the embedded preview extracted with exiftool into `_cull/previews/`, falling
back to an ffmpeg conversion.

**Thumbnails**: one JPEG per item (frame at 10 % of the duration for video).

Output: `<work>/_cull/items.json`, `thumbs/`, `proxies/`, `previews/`,
`probe-cache.json`, `geocache.json`.

## 3. Review UI

`cull review` serves the viewer (stdlib server, port 8778 by default so it can
run next to teslacam's 8777).

- **Grid** of thumbnails grouped by day, and inside a day by session (a gap
  of more than 2 h starts a new session). Each tile shows the time,
  duration, camera model, keep/drop state, trip name, and chips for
  `in Immich`, `time-from-mtime` and `no proxy`.
- **Viewer pane**: player (or image) for the current item, with its file
  list (master + companions), place, and the Immich match path if any.
- **Keys**: `k` keep, `d` drop, `u` unreviewed, ←/→ previous/next item,
  `space` play/pause, `j`/`l` ±5 s, `t` set trip on the selection, `n` note.
  Marking an item advances to the next one.
- **Selection**: click selects, shift-click selects a range, a day or session
  header selects its items. `k`/`d`/`t` apply to the whole selection.
- **Set trip…**: a text field with suggestions — the existing folder names
  under `--trips` (if given) whose date prefix overlaps the selection, and a
  new name `YYYY-MM-<place-slug>` built from the selection's first date and
  most common place. Names must match `^[0-9]{4}-[0-9]{2}(-[a-z0-9]+)+$`.
- **Tags** (comma-separated, free text) and a **note** per item; editing on a
  multi-selection adds tags to every item and sets the note on every item.
- **Filters**: unreviewed / keep / drop / in Immich / kept without trip, plus
  a per-source toggle. A counter shows reviewed/total and kept size in GB.

Decisions: `<work>/_cull/decisions.json`,
`{item id: {keep, trip, tags, note, updated}}`, written atomically under a
lock on every change. Defaults: `keep=unreviewed`, no trip, no tags, empty
note. Items already in Immich are not auto-dropped; the chip is a hint.

## 4. Stage

`cull stage --work DIR --trips /mnt/tank/media/trips` plans by default and
acts only with `--yes`.

For every item with `keep=keep`:

- **No trip** → the item is listed and stage stops before touching anything.
- Destination: `trips/<trip>/<file name>` for the master and each companion,
  original names unchanged.
- Already there as the same inode → skipped (re-running is safe).
- A different file already at that name (two DJI_0001.MP4 from different
  sources in one trip) → the whole item goes to
  `trips/<trip>/<src name>/` instead; if that is also taken, stop with the
  conflict listed.
- Hardlink only. If source and destination are on different filesystems
  (`os.link` → EXDEV, checked up front with `st_dev`), stage refuses; no copy
  fallback.
- **Tags and note**: when the item has any, they are written to the master's
  own sidecar `trips/<trip>/<name>.<ext>.xmp` (CLAUDE.md: a stem-named
  `name.xmp` is picked up by every file with that stem) with exiftool:
  `XMP-dc:Subject` and `XMP-lr:HierarchicalSubject` added to (Immich reads
  either), `XMP-dc:Description` set when the note is non-empty. If the source
  already has an `.xmp` companion, the trip gets a **copy** of it (not a
  hardlink, so the source is never edited) under its own name, and the tags
  are merged into the master's `<name>.<ext>.xmp` (created from that copy when
  it was stem-named). `immy promote` runs every sidecar through
  `sidecar_check`, so a bad merge is caught before Immich sees it.

Output: a plan table (trip, files, GB, links, skips, sidecars), then on `--yes`
the actions, and `trips/<trip>/.audit/cull-staged.json` listing what was
linked from where. Stage never creates trip notes (`immy audit` makes the
README on its first run) and never deletes anything.

`cull report --dropped` prints the dropped items' source paths (master +
companions) and their total size, for cleaning up the gdrive staging copy by
hand later.

## 5. Errors

- Unreadable or unprobeable file: the item is kept in the list with an
  `unreadable` chip and cannot be staged until it probes cleanly.
- Proxy generation failure: the item gets `no proxy`; review still works on
  the thumbnail.
- `--immich` with no DB reachable: scan fails before probing (the flag was
  asked for explicitly).
- Server: same rules as teslacam — escapes rejected, unknown item ids and
  invalid trip names → 400, decisions written atomically.

## 6. Tests

pytest, fixtures generated with ffmpeg/exiftool in `tmp_path`:

- grouping: DJI MP4 + SRT + LRF → one item; DNG + JPG → one item; orphan LRF
  reported; `._*` ignored.
- playback choice: LRF used, H.264 master served, HEVC master → proxy made.
- capture time from SRT, EXIF, QuickTime, mtime fallback flagged.
- stage: plan without `--yes` changes nothing; `--yes` links (same inode);
  re-run skips; name collision → src subfolder; kept-without-trip stops;
  EXDEV refused (monkeypatched `st_dev`); xmp written, existing xmp copied and
  merged, source xmp untouched.
- server: Range 206 responses and escape rejection (moved with the core),
  decision update and trip-name validation.
- host: `--host tailscale` picks the 100.64.0.0/10 address from a stubbed
  interface list and errors when there is none.
- teslacam: the full existing suite passes after the core extraction.

## Out of scope

Trimming, renders, re-encoding of kept files, deleting sources, writing to
Immich, and auto-dropping files already in Immich.

## First use

```sh
cull scan --work /mnt/tank/media/staging/gdrive-review --immich \
  --src ".../unsorted1/Cine" --src ".../unsorted1/audiq7" \
  --src ".../Desktop - m1max/Incoming - Drone+La Manga" --src <vd-1 dir>
cull review --work ... --host tailscale
cull stage  --work ... --trips /mnt/tank/media/trips --yes
# then per trip: immy audit → immy srt → immy promote (in the n5 container)
```
