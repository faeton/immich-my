# teslacam

Tesla dashcam dumps → reviewed, trimmed, stack-ready render folders that
`immy promote` ships and `immy stacks apply` turns into Immich stacks.
Design: [todo/TESLA-STACKS-DESIGN.md](../todo/TESLA-STACKS-DESIGN.md).

Standalone on purpose: it re-encodes, reads protobuf telemetry and runs a
local web viewer, none of which belong next to immy's immutable-originals
rules. The raw dumps are never modified.

```sh
cd teslacam && uv sync
uv run teslacam scan   --root ~/Media/inbox/TeslaDumps          # → <root>/_review/events.json (+thumbs, geocache)
uv run teslacam seed   --root ~/Media/inbox/TeslaDumps --tags-json ~/Media/inbox/TeslaDumps/tags.json   # once
uv run teslacam review --root ~/Media/inbox/TeslaDumps          # http://127.0.0.1:8777 — keep/drop, cameras, in/out
uv run teslacam render --root ~/Media/inbox/TeslaDumps --out ~/Media/render --model "Model Y"
# then, from immy/:
uv run immy promote ~/Media/render/2026-tesla-dashcam --into-album "Tesla Dashcam"
uv run immy stacks apply ~/Media/render/2026-tesla-dashcam --write
```

## What each step does

- **scan** walks `<root>/<dump>/{SavedClips,SentryClips}/<event>/`, the flat
  2024 layout `<dump>/<ts>__city__reason/`, and `RecentClips` (grouped into
  drives on gaps > 120 s, chapters ≤ 15 min). Per event: segment map, ffprobe
  durations/clocks (cached), SEI GPS (median fix of the trigger segment),
  `event.json` fallback, previous-drive fallback (< 6 h), timezone from GPS
  (`timezonefinder`) or the car's clock minus the file's UTC clock, optional
  Nominatim city (cached, seeded from the old `_tools/geocache.json`),
  trigger-frame thumbnails. Duplicate event folders across dumps are skipped.
  `--exclude TeslaCam` is the default (the still-encrypted USB originals).
- **review** serves the viewer (stdlib HTTP server with Range requests).
  Decisions go to `<review>/decisions-<dump>.json`: `keep`
  (unreviewed|keep|drop), `cameras` (ticked subset), `in_s`/`out_s`,
  `precise`, `tags`, `note`, `archive`. Default window: Saved
  `[trigger−180 s, +30 s]`, Sentry `±60 s`, drives whole.
- **render** writes `<out>/<YYYY>-tesla-dashcam/<kind>-<car folder stamp>/`:
  `mosaic.mp4` (all cameras tiled, exact window, H.264 proxy at 720 rows so
  Immich plays it without transcoding), one `<camera>.mp4` per ticked camera
  (lossless `-c copy` concat of the whole segments overlapping the window,
  wall-clock subtitle track with the `>>> EVENT <<<` marker; `precise`
  re-encodes to the exact range), `track.gpx`/`track.json` from SEI,
  `event.json`, and a `render.json` receipt. Metadata is baked into every mp4
  with exiftool: `Keys:CreationDate` local+offset, `QuickTime:CreateDate` UTC,
  Make/Model, description; GPS on the mosaic only. The folder's `stacks.json`
  lists primary, children, tags and the archive flag per event. Idempotent:
  the receipt's digest (segments + decision + renderer version) skips
  unchanged events; a dropped event's folder is reported and removed with
  `--prune`. One confirmation per dump (`--yes` skips, `--dry-run` plans).

Folder names never carry the city or reason: in an Immich external library
the path is the asset's identity, so a later city/timezone fix must not
move the file.

## Tests

```sh
cd teslacam && uv run pytest     # ffmpeg/ffprobe/exiftool needed for the render tests
```
