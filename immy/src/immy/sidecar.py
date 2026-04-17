"""XMP sidecar writer via exiftool.

We write standalone `.xmp` files next to media (never touch originals).
Existing sidecars are merged (exiftool default), so re-running the same
rule with the same patch is a no-op.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


EXIFTOOL = "exiftool"


def _sidecar_path(media: Path) -> Path:
    """Adobe standard: `basename.xmp` (e.g. DSC_4182.xmp for DSC_4182.JPG).

    This collapses stream-pairs sharing a stem (Live Photo HEIC+MOV) into
    one sidecar, which is correct — they share capture metadata. Cross-
    type collisions in a single folder are rare in practice and would be
    surfaced as a rule conflict.
    """
    return media.with_suffix(".xmp")


def write(media: Path, patch: dict[str, object]) -> Path:
    """Write `patch` into the XMP sidecar for `media`. Returns sidecar path.

    `patch` keys are exiftool tag names (e.g. `GPSLatitude`, `GPSLatitudeRef`,
    `DateTimeOriginal`, `HierarchicalSubject`). Values are stringified.
    List values become repeated assignments (`=`) which overwrite the list
    in the sidecar — idempotent when the same list is re-applied.
    """
    sidecar = _sidecar_path(media)
    args = [EXIFTOOL, "-overwrite_original", "-q", "-q"]
    for tag, value in patch.items():
        if isinstance(value, (list, tuple)):
            # First `=` clears the list; subsequent `=` append entries.
            args.append(f"-XMP:{tag}=")
            for item in value:
                args.append(f"-XMP:{tag}={item}")
        else:
            args.append(f"-XMP:{tag}={value}")
    args.append(str(sidecar))
    # If sidecar doesn't exist yet, exiftool creates it when we write XMP tags
    # to a .xmp path directly.
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"exiftool failed for {media.name}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return sidecar


def read(media: Path) -> dict[str, str]:
    """Read the sidecar's XMP as a flat dict. Empty dict if no sidecar."""
    sidecar = _sidecar_path(media)
    if not sidecar.is_file():
        return {}
    result = subprocess.run(
        [EXIFTOOL, "-j", "-n", "-G0", str(sidecar)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {}
    import json
    blobs = json.loads(result.stdout)
    if not blobs:
        return {}
    return {k.split(":", 1)[-1]: v for k, v in blobs[0].items() if k != "SourceFile"}
