"""Detect files exported/edited through a desktop app that dropped
DateTimeOriginal in the process.

Canonical example: Lightroom / Photos export pipeline copies the image,
stamps `ModifyDate` with the export instant, and (depending on the export
preset) may not carry DateTimeOriginal forward. If that file lands in
Immich without capture metadata, it appears in the timeline at the export
date — months or years after it was actually shot. The file *looks* dated
but it's lying.

LOW-confidence: we can't automatically guess the true capture instant,
but the user deserves a flag so they know this file won't sort correctly.
Fix is manual — re-export with the original EXIF retained, or drop the
asset. `action="note"` so the file surfaces in the per-file flags column
without proposing a patch.

Condition: ModifyDate is present AND DateTimeOriginal / CreateDate are
both absent. We deliberately do NOT flag the common "exported but EXIF
intact" case (both present, possibly with ModifyDate ≫ DateTimeOriginal)
— that file still sorts correctly in Immich.
"""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from .registry import Finding, Rule, register


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        dto = row.get(
            "XMP:DateTimeOriginal", "EXIF:DateTimeOriginal",
            "QuickTime:CreateDate", "EXIF:CreateDate",
        )
        if dto:
            continue
        md = row.get("XMP:ModifyDate", "EXIF:ModifyDate")
        if not md:
            continue
        out.append(Finding(
            rule="export-date-trap",
            confidence="low",
            path=row.path,
            action="note",
            reason=f"ModifyDate={md} but no DateTimeOriginal — likely an export that stripped capture date",
        ))
    return out


register(Rule(name="export-date-trap", confidence="low", propose=_propose))
