"""Capture-device (make/model) resolution.

Immich shows `asset_exif.make` / `model` in the info panel and lets you
filter by them, but several cameras don't fill the standard EXIF/QuickTime
Make+Model fields — so immy backfills from the places they *do* write:

- **DJI video** leaves Make/Model empty; the real model sits in the mp4
  `ItemList:Encoder` atom (e.g. `DJIMavic3Cine`). Exported/re-encoded
  clips carry a generic muxer string there (`Lavf…`, `libav…`) which is
  NOT a device — those are ignored.
- **DJI stills** report a bare module code as Model (`FC4170`, `L2D-20c`).
- Both get mapped to friendly names so "DJI Mini 4 Pro" is filterable,
  not `FC4170`.

Insta360 is handled separately in `exif.py` (vendor trailer, per-trip
cache) because it needs a slow `-ee` trailer parse.
"""

from __future__ import annotations

import re

# Module code (still EXIF Model) / mp4 Encoder string → (make, friendly model).
# Verified against the real corpus; extend as new gear shows up.
_DJI_MODELS: dict[str, tuple[str, str]] = {
    # Owner-confirmed gear (camera module code → drone/cam).
    "FC3170": ("DJI", "DJI Mavic Air 2"),
    "FC3582": ("DJI", "DJI Mini 3 Pro"),
    "FC8482": ("DJI", "DJI Mini 4 Pro"),
    "FC9313": ("DJI", "DJI Mini 5 Pro"),
    "L2D-20c": ("DJI", "DJI Mavic 3"),       # Hasselblad (wide) cam on the Mavic 3
    "FC4170": ("DJI", "DJI Mavic 3 Tele"),   # tele cam on the same Mavic 3 (diff focal length)
    "AC002": ("DJI", "DJI Osmo Action 3"),   # action cam, not a drone
    # Video Encoder atoms (DJI writes these instead of Make/Model).
    "DJIMavic3Cine": ("DJI", "DJI Mavic 3 Cine"),
    "DJI Mini4 Pro": ("DJI", "DJI Mini 4 Pro"),
    "DJI Mini3 Pro": ("DJI", "DJI Mini 3 Pro"),
}
# Case-insensitive view so a lowercased/variant code still maps.
_DJI_MODELS_CI = {k.lower(): v for k, v in _DJI_MODELS.items()}

# Generic muxer/encoder strings that are NOT a device — seen in the
# Encoder atom of exported/transcoded clips.
_GENERIC_ENCODER = re.compile(r"^(lavf|libav|x26[45]|ffmpeg|handbrake|gopro)", re.I)


def is_device_encoder(encoder: str | None) -> bool:
    """True if an mp4 Encoder string names a real device, not a muxer."""
    enc = (encoder or "").strip()
    return bool(enc) and not _GENERIC_ENCODER.match(enc)


def resolve(
    make: str | None, model: str | None, encoder: str | None = None,
) -> tuple[str | None, str | None]:
    """Best-effort (make, model) for an asset.

    Precedence: an explicit EXIF/QuickTime model wins; otherwise fall back
    to a device-bearing mp4 Encoder atom (DJI video). Known DJI module
    codes / encoder strings are mapped to friendly names and gain
    `make="DJI"` when make was blank.
    """
    make = (make or "").strip() or None
    model = (model or "").strip() or None

    if model is None and is_device_encoder(encoder):
        model = encoder.strip()

    if model is not None:
        mapped = _DJI_MODELS.get(model) or _DJI_MODELS_CI.get(model.lower())
        if mapped is not None:
            mapped_make, model = mapped
            make = make or mapped_make
        elif model.upper().startswith(("FC", "L2D", "DJI")) and make is None:
            # Unmapped DJI code — at least set the make so it's filterable.
            make = "DJI"

    return make, model


__all__ = ["resolve", "is_device_encoder"]
