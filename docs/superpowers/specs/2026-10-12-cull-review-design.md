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
uv run cull review --work DIR [--trips /mnt/tank/media/trips] [--host 127.0.0.1|tailscale|ADDR] [--port 8778]
uv run cull stage  --work DIR --trips /mnt/tank/media/trips [--yes]
uv run cull report --work DIR [--dropped|--kept|--unreviewed]
```

### Host binding

`--host` defaults to `127.0.0.1`. `--host tailscale` binds to this machine's
Tailscale address: the IPv4 address in 100.64.0.0/10 on an interface named
`tailscale*` (`tailscale0` 100.115.236.50 on n5; the host has no `tailscale`
CLI, so the interfaces are read directly — Docker bridges or other VPNs in
the same CGNAT range are not considered). Not exactly one such address →
exit with an error. `0.0.0.0` and `::` are refused; any other address is
bound as given. The
printed URL uses the bound address.

The server has no login; on the Tailscale address every device on the
tailnet can reach it, which is the trust boundary (the tailnet is the
user's own devices). To keep a browser on another site from writing
decisions, POSTs must carry `Content-Type: application/json`, and requests
whose `Host` is not the bound address/port or whose `Origin` is present
and different are rejected with 403. The same checks go into the core, so
teslacam gets them too, and `teslacam review` gains the same `--host`
flag. Media responses use the right type for every file served, `.lrf` and
proxies as `video/mp4`.

## 2. Scan → items

`cull scan` walks each `--src` and builds review items. Sources are read only.

**Grouping.** An item is one master plus its companions: files in the same
directory with the same stem (case-insensitive). Masters: video
(`.mp4 .mov .m4v .avi .mts`) and stills (`.jpg .jpeg .heic .png .dng` and
other raw). Companions: `.srt .lrf .thm .xmp .aae`, matched by stem, plus a
`<name>.<ext>.xmp` sidecar of the master. Same-stem stills — a DNG+JPG pair (Mavic 3 Cine
shoots both) or a HEIC/JPG+MOV live photo — are one item: one shot, one
decision, all files staged; the JPG/HEIC is the preview and gives the
metadata. A video and a still with the same stem are otherwise separate
items. An `.lrf`/`.srt` with no master is reported as an orphan
and not an item; orphans are listed in the grid (own filter) and in
`cull report`. Symlinks are not followed and not staged. AppleDouble `._*` files and `.DS_Store` are ignored.

**Sources and ids.** Each `--src` is registered once in `items.json` as
`{name: absolute path}`; the name is the directory's basename (`-2`, `-3`
appended when taken) and stays with that path on later scans whatever the
argument order. Item id = `<src name>/<path of the master relative to the
src>`. Each item stores a fingerprint (size + mtime of master and
companions); the decision stores the fingerprint it was made on. If a file
changes under the same path, the decision is shown as `changed` and stage
refuses that item until it is reviewed again.

**Per item** (cached by path + size + mtime, so a re-scan only probes new or
changed files):

- kind (video/photo), size, duration, resolution, codec (ffprobe; exiftool for
  stills).
- capture time, approximate: the wall-clock date-time in the first SRT cue
  body (`2024-01-15 14:30:22,123`, `…22.123`, or Mavic Pro
  `2017.08.05 14:11:51`; the cue line `00:00:00,000 --> …` is an offset,
  not a time; local, no zone); else EXIF `DateTimeOriginal`; else QuickTime
  `CreationDate`; else `CreateDate` read the way immy reads it (UTC unless
  the make is in `capture.QUICKTIME_LOCAL_CLOCK_MAKES`); else file mtime
  (flagged `time-from-mtime`). It is
  used only for sorting, grouping and suggesting names; the real capture time
  is immy audit's job.
- GPS: first valid fix in the SRT (`0,0` and out-of-range values skipped), else EXIF GPS. Place: reverse-geocoded
  city through a Nominatim cache in `_cull/geocache.json` (1 req/s, offline
  works from cache; `--no-geocode` skips).
- capture-time provenance: which source gave the time, and whether it is a
  local wall clock (SRT, EXIF without offset), an instant (QuickTime UTC,
  EXIF with offset) or a guess (mtime).
- **possibly in Immich** (only with `--immich`; read-only queries against
  the Immich DB from `config.host.yml`, re-run for every item on every scan
  so the hint never goes stale; DB unreachable → warning, no chips). External-library checksums here
  are path hashes (`sha1-path` on all 230k external assets), so content
  hashes cannot be compared. Candidates: a video with duration within 1 s
  and a time within 5 s; a still with the same file size and a time within
  2 s. Wall-clock times compare against `localDateTime`, instants against
  `fileCreatedAt`; mtime-guessed times are not matched. Every candidate's
  original path is listed (there can be several). It is a hint for the
  reviewer, never a decision.

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
  `possibly in Immich`, `changed`, `time-from-mtime` and `no proxy`.
- **Viewer pane**: player (or image) for the current item, with its file
  list (master + companions), place, and the Immich candidate paths if any.
- **Keys**: `k` keep, `d` drop, `u` unreviewed, ←/→ previous/next item,
  `space` play/pause, `j`/`l` ±5 s, `t` set trip on the selection, `n` note.
  Marking an item advances to the next one.
- **Selection**: click selects, shift-click selects a range, a day or session
  header selects its items. `k`/`d`/`t` apply to the whole selection.
- **Set trip…**: a text field with suggestions — the existing folder names
  under `--trips` (if given) whose date prefix overlaps the selection, and a
  new name `YYYY-MM-<place-slug>` (the convention of the existing trips,
  e.g. `2023-08-croatia`) from the selection's first date and most common
  place. A name is accepted if it is an existing trip folder, or a new one
  matching `^[0-9]{4}-[0-9]{2}(-[0-9]{2})?(-[a-z0-9]+)+$`.
- **Tags** (comma-separated, free text) and a **note** per item; editing on a
  multi-selection adds tags to every item and sets the note on every item.
- **Filters**: unreviewed / keep / drop / possibly in Immich / kept without trip, plus
  a per-source toggle. A counter shows reviewed/total and kept size in GB.

Decisions: `<work>/_cull/decisions.json`,
`{item id: {keep, trip, tags, note, updated}}`, written atomically under a
lock on every change. Defaults: `keep=unreviewed`, no trip, no tags, empty
note. Items possibly in Immich are not auto-dropped; the chip is a hint.

## 4. Stage

`cull stage --work DIR --trips /mnt/tank/media/trips` plans by default and
acts only with `--yes`.

**Layout.** Files go to `trips/<trip>/<src name>/<path relative to the
src>`, original names unchanged. Keeping the source's directories means two
unrelated `DJI_0001.*` groups never share a directory (immy pairs `.SRT` and
`.xmp` with media by stem, so a flattened folder could hand one clip's
telemetry to another file). immy audit and promote walk trip folders
recursively, so subfolders are fine.

**Plan first.** Stage works per trip: it builds the whole plan for a trip
(collisions inside the run included), checks it, and changes nothing in that
trip if any check fails. Kept items with no trip, and trips that failed a
check, are listed and the command exits non-zero; complete trips still
stage. Checks:

- the trip's items have decisions that are not `changed` and files that
  probe cleanly;
- every source file and `trips/` are on one filesystem (`st_dev` per
  file); no copy fallback; a link error still undoes the links made for
  that item;
- a destination path that already exists must be the same inode as its
  source (then it is skipped), else stop with the conflict listed;
- no destination path is already an Immich asset's `originalPath` (read-only
  DB check, mapped to the container path; skipped with a warning if the DB
  is down);
- the trip's receipt (below) agrees with the decisions: an item it lists
  that is now dropped or assigned to another trip is reported as stale,
  and stage stops until the stale links are removed by hand (stage never
  deletes).

**Links.** With `--yes`, the master and every companion are hardlinked,
`.xmp` included. On n5 immy writes its own sidecars into the `/sidecars`
mirror, never next to trip files, so nothing downstream writes through a
link into the source.

**Receipt.** `trips/<trip>/.audit/cull-staged.json`, written atomically
after the links: per item its id, source paths, destination paths,
fingerprint, tags and note. Re-running with unchanged decisions does
nothing.

**Tags and note are not written to any sidecar.** immy reads stem-named
`name.xmp` sidecars and rewrites their tag lists in audit, so a cull-written
sidecar would compete with it. They stay in `decisions.json` and the
receipt; applying them in Immich is a later step, not part of this build.

Output: a plan table (trip, items, files, GB, links, skips, stale), then on
`--yes` the actions. Stage never creates trip notes (`immy audit` makes the
README on its first run) and never deletes anything.

`cull report --dropped` prints the dropped items' source paths (master +
companions) and their total size, for cleaning up the gdrive staging copy by
hand later.

## 5. Errors

- Unreadable or unprobeable file: the item is kept in the list with an
  `unreadable` chip and cannot be staged until it probes cleanly.
- Proxy generation failure: the item gets `no proxy`; review still works on
  the thumbnail.
- `--immich` with no DB reachable: a warning, and no Immich chips.
- Server: same rules as teslacam — escapes rejected, unknown item ids and
  invalid trip names → 400, decisions written atomically.

## 6. Tests

pytest, fixtures generated with ffmpeg/exiftool in `tmp_path`:

- grouping: DJI MP4 + SRT + LRF → one item; DNG + JPG → one item; orphan LRF
  reported; `._*` ignored.
- playback choice: LRF used, H.264 master served, HEVC master → proxy made.
- capture time from SRT (Mavic 3 comma/dot and Mavic Pro formats; the cue
  arrow line ignored), EXIF, QuickTime, mtime fallback flagged; SRT `0,0`
  GPS skipped.
- live photo (HEIC+MOV) and DNG+JPG are one item each; symlinks skipped.
- stage: plan without `--yes` changes nothing; `--yes` links (same inode)
  under `<trip>/<src>/<rel dir>`; re-run is a no-op; a different file at a
  destination stops before any link; kept-without-trip and `changed` stop;
  another filesystem refused (monkeypatched `st_dev`); an item dropped or
  moved after staging is reported stale and stops; receipt contents.
- server: Range 206 responses and escape rejection (moved with the core),
  Host/Origin/Content-Type rejection, decision update and trip-name
  validation.
- ids: swapped `--src` order keeps ids; a changed file marks its decision
  `changed`.
- host: `--host tailscale` picks the 100.64.0.0/10 address on `tailscale*`
  from a stubbed interface list, ignores the same range on `docker0`, errors
  on none or several; `0.0.0.0` refused.
- stage per trip: a trip with a conflict is untouched while a complete trip
  stages, exit code non-zero; a destination already an Immich
  `originalPath` stops its trip (stubbed DB).
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
# then per trip, in the n5 container (paths as the container sees them):
#   immy audit /trips/<trip> --write
#   immy srt track /trips/<trip>
#   immy promote /trips/<trip>
#   immy srt geotag /trips/<trip>       # needs the promoted assets
```
