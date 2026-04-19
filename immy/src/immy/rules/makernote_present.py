"""Flag files carrying vendor MakerNote blocks.

MakerNote is a vendor-specific EXIF IFD that can embed serial numbers,
shutter counts, focus-point coordinates, and other details the camera
maker decided to ship. Privacy-conscious users may want them gone before
publishing sidecars. The block lives inside the original file's EXIF —
not in an XMP sidecar — so stripping it means editing the original,
which violates immy's sidecar-only invariant.

This rule only *notes* the presence. The reason line carries the
exiftool command the user can run themselves if they want to strip.
Confidence: LOW — purely advisory, never auto-applied.
"""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from .registry import Finding, Rule, register


def _has_makernote(row: ExifRow) -> bool:
    return any(k.startswith("MakerNotes:") for k in row.raw)


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        if not _has_makernote(row):
            continue
        out.append(Finding(
            rule="makernote-present",
            confidence="low",
            path=row.path,
            action="note",
            reason="MakerNote block present; strip with `exiftool -overwrite_original -MakerNotes= <file>` if privacy matters",
        ))
    return out


register(Rule(name="makernote-present", confidence="low", propose=_propose))
