# cull

Import candidates (drone masters, phone videos, photos) → reviewed in a
browser → kept files hardlinked into trip folders for `immy audit` →
`immy srt` → `immy promote`. Sources are never modified, moved or deleted.
Design: [docs/superpowers/specs/2026-10-12-cull-review-design.md](../docs/superpowers/specs/2026-10-12-cull-review-design.md).

```sh
cd cull && uv sync --extra immich
uv run cull scan   --work W --src DIR [--src DIR ...] [--immich]   # → W/_cull/items.json (+ thumbs, proxies)
uv run cull review --work W --trips /mnt/tank/media/trips --host tailscale   # http://<tailnet ip>:8778
uv run cull stage  --work W --trips /mnt/tank/media/trips --immich           # plan
uv run cull stage  --work W --trips /mnt/tank/media/trips --immich --yes     # link
uv run cull report --work W --dropped                                        # paths to clean up by hand
```

- **Items**: one shot = the primary file and everything with its stem in the
  same folder (`.SRT`, `.LRF`, `.THM`, `.AAE`, its `.xmp`); DNG+JPG and live
  photos are one item. Orphan companions are listed, not items.
- **Times** are approximate (SRT wall clock, EXIF, QuickTime as immy reads
  it, else mtime — flagged); they sort, group and name trips. immy audit owns
  the real capture time.
- **In Immich?** is a hint (time + duration or size); external-library
  checksums are path hashes, so contents cannot be compared. An Immich DB that
  fails mid-scan only produces a warning.
- **Stage** links into `trips/<trip>/<source>/<path in source>`, per trip,
  all checks first (same filesystem, no foreign file at a destination, not
  already an Immich path, receipt agrees with the decisions). It refuses a trip
  whose folder (or any folder under it) is a symlink, trip names that aren't
  plain folder names, and destinations that differ only by letter case; kept
  items no longer in the scan are reported. The receipt is updated after every
  linked item. Receipt: `trips/<trip>/.audit/cull-staged.json`. Tags and notes
  stay in the decisions and the receipt — cull writes no sidecars.
- **Viewer** has no login. `--host tailscale` binds the tailnet address only
  (never 0.0.0.0); POSTs need JSON and a matching Origin.

## Tests

```sh
cd cull && uv run pytest     # ffmpeg/ffprobe/exiftool needed for the media tests
```
