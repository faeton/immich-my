# Google Takeout dates

Google Takeout strips most EXIF and ships each file's capture time and
position in a JSON companion. Two things go wrong with that, and
`immy takeout redate` repairs both in an existing library.

## The JSON naming trap

Takeout names the JSON after the *original* file, moves any duplicate counter
to the very end, and truncates long names:

| Media file             | Its JSON                                      |
|------------------------|-----------------------------------------------|
| `IMG_0001.JPG`         | `IMG_0001.JPG.supplemental-metadata.json`     |
| `IMG_0001(1).JPG`      | `IMG_0001.JPG.supplemental-metadata(1).json`  |
| `IMG_0001(1).MP4`      | `IMG_0001.HEIC.supplemental-metadata(1).json` (a Live Photo's video shares its still's JSON) |
| `IMG_0001-edited.JPG`  | `IMG_0001.JPG.supplemental-metadata.json`     |

`dedup.engine._google_json_companion` matches sibling JSONs on what they say
about themselves, their `title` and trailing counter, never on a filename
prefix. An older prefix lookup missed every `(n)` file. Those came in
dateless, and a folder-year fallback stamped them `YYYY-01-01 12:00:00`.

## The UTC clock

`photoTakenTime` is a UTC epoch. Written as `+00:00`, Immich shows the right
instant on a UTC clock: hours off, and near midnight on the wrong day. The
rescue now writes the instant in the zone at the photo's position, when it has
one.

## `immy takeout redate`

```sh
immy takeout redate \
  --manifest /state/manifest.sqlite \
  --takeout-root /path/to/takeout \       # the tree the manifest called --staging-prefix
  --originals /path/to/library \          # the library import path, as seen from here
  --csv plan.csv                          # dry run: the per-asset plan
# …review, then:
immy takeout redate … --asset <id> --apply   # pilot one asset
immy takeout redate … --apply
```

**Which assets.**
- Placeholder-dated: an on-the-hour time shared by `--placeholder-min` (10)
  or more assets.
- With `--utc` (the default), Takeout assets Immich shows in UTC.

**Finding each source.** Promote put every file at a path derived from its
staging path and date (`_promote_dest`, or its `__<id>` collision name). Each
asset maps back to its Takeout file exactly; a name guess is never needed.
Several candidates are narrowed by consistency, and exactly one must remain:
- A placeholder's source sits in `Photos from <its year>`.
- A UTC asset must currently show Google's instant to within 2 s.

**The date.**
1. The Takeout JSON.
2. Else the file's own embedded capture time, when absolute.
3. Else interpolation between numbered neighbours of the same extension in the
   same Takeout folder, on both sides, agreeing to within two days.

**The zone.**
1. The file's GPS.
2. Else the JSON's `geoData`.
3. Else the zone most shots within 3 h (then 24 h) carry.
4. Else UTC.

**Left alone.**
- A UTC asset whose own date disagrees with Google's. That's usually a camera
  clock (GoPro). The file's word is not overruled by Google's.
- A UTC asset for which no zone can be found.

**Writing.**
1. `DateTimeOriginal` with its offset goes into the asset's XMP sidecar.
2. The sidecar is registered on the asset in `asset_file`, which is what
   Immich's SidecarCheck job does. A per-asset metadata refresh reads only a
   registered sidecar.
3. Immich refreshes metadata for exactly those assets.

Originals are never touched. Every change goes to
`takeout-redate-<time>.jsonl` under `state_root`: the sidecar's previous text,
the previously registered sidecar, and the old and new dates.

**Copies.** `--stack-copies` (the default) stacks a re-dated Takeout copy
(`IMG_1711(1).MP4`) onto the library original it duplicates, with the original
as primary. A twin has:
- the original file name;
- the same instant to the second, or the same minutes and seconds a whole
  number of hours apart (an original stored in the wrong zone);
- no CLIP disagreement.

Google re-encodes, so bytes never match.
