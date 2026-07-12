"""Apply `tags:` list from folder notes file front-matter as XMP
HierarchicalSubject + Subject on every media asset.

HIGH because the tags are explicit user intent (typed in the notes file).
"""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from ..notes import join_make_model, parse_frontmatter, resolve
from .registry import Finding, Rule, register


# Filename-prefix → vendor heuristic for files that carry no
# Make/Model in their container (DJI drones write metadata to .SRT
# sidecars; GoPro splits across MP4 atoms exiftool doesn't surface
# without -ee; etc.). Used only when EXIF/QuickTime are empty.
_FILENAME_HINTS: tuple[tuple[str, str], ...] = (
    ("DJI_", "DJI"),
    ("GOPR", "GoPro"),
    ("GX01", "GoPro"),
    ("GH01", "GoPro"),
)


def file_camera(row: ExifRow) -> str | None:
    """Return canonical "<Make> <Model>" for this file, or None.

    Insta360 trailer fields are populated into QuickTime:Make/Model by
    `exif.read_folder`, so .insv/.lrv flow through the same path. For
    other vendors that don't surface Make/Model at all (DJI MP4, GoPro)
    we fall back to a filename-prefix heuristic.
    """
    make = row.get("EXIF:Make", "QuickTime:Make", "QuickTime:AndroidMake") or ""
    model = row.get("EXIF:Model", "QuickTime:Model", "QuickTime:AndroidModel") or ""
    cam = join_make_model(make, model)
    if cam:
        return cam
    name = row.path.name
    for prefix, vendor in _FILENAME_HINTS:
        if name.startswith(prefix):
            return vendor
    return None


def tags_for_file(cam: str | None, tags: list[str]) -> list[str]:
    """Resolve the trip's notes `tags:` list down to the ones that apply to
    one file, given its camera (`file_camera`). Split out so both the XMP
    rule below and `tagsync.py` (native Immich Tag API, for video assets
    XMP never reaches) agree on exactly which tags land on which file.

    Gear/Camera/* tags must only land on files whose own camera matches —
    otherwise an Insta360 .insv ends up tagged "Gear/Camera/Canon EOS R7"
    just because a Canon was also on the trip.
    """
    gear_tags = [t for t in tags if t.startswith("Gear/Camera/")]
    base_tags = [t for t in tags if not t.startswith("Gear/Camera/")]
    per_file = list(base_tags)
    matched = False
    if cam:
        for gt in gear_tags:
            gt_cam = gt.removeprefix("Gear/Camera/").strip()
            # Match if either side is a substring of the other —
            # handles "Insta360" vs "Insta360 X3", "Canon EOS R7"
            # vs "Canon Canon EOS R7", etc.
            if gt_cam and (gt_cam in cam or cam in gt_cam):
                per_file.append(gt)
                matched = True
    # If the file's camera doesn't match any notes-listed gear tag
    # (e.g. notes are stale, or first run after adding a new device),
    # synthesize one from the file's own metadata so the asset is
    # attributed to its actual device rather than falling through to
    # the trip name in display fallbacks.
    if not matched and cam:
        per_file.append(f"Gear/Camera/{cam}")
    return per_file


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    tags = fm.get("tags") or []
    tags = [t for t in tags if isinstance(t, str) and t.strip()]
    if not tags:
        return []

    out: list[Finding] = []
    for row in rows:
        cam = file_camera(row)
        per_file = tags_for_file(cam, tags)
        if not per_file:
            continue
        subjects = sorted({t.split("/")[-1] for t in per_file})
        out.append(Finding(
            rule="trip-tags-from-notes",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch={
                "HierarchicalSubject": per_file,
                "Subject": subjects,
            },
            reason=f"{len(per_file)} tag(s) from {notes.name} front-matter",
        ))
    return out


register(Rule(name="trip-tags-from-notes", confidence="high", propose=_propose))
