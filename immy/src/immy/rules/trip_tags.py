"""Apply `tags:` list from folder notes file front-matter as XMP
HierarchicalSubject + Subject on every media asset.

HIGH because the tags are explicit user intent (typed in the notes file).
"""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from ..notes import parse_frontmatter, resolve
from .registry import Finding, Rule, register


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    tags = fm.get("tags") or []
    tags = [t for t in tags if isinstance(t, str) and t.strip()]
    if not tags:
        return []

    subjects = sorted({t.split("/")[-1] for t in tags})

    out: list[Finding] = []
    for row in rows:
        out.append(Finding(
            rule="trip-tags-from-notes",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch={
                "HierarchicalSubject": tags,
                "Subject": subjects,
            },
            reason=f"{len(tags)} tag(s) from {notes.name} front-matter",
        ))
    return out


register(Rule(name="trip-tags-from-notes", confidence="high", propose=_propose))
