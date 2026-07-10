"""Perceptual hash (pHash) for the dedup cascade's Stage B prefilter.

64-bit DCT hash, computed from a 32×32 grayscale squash of the image
(EXIF orientation applied by libvips before the resize). Classic pHash:
2-D DCT-II, keep the 8×8 low-frequency block, threshold each coefficient
against the median of the 63 AC coefficients (DC excluded from the
median — it's pure brightness and would skew the threshold).

We implement the DCT with a precomputed numpy basis matrix instead of
adding an `imagehash`/`scipy` dependency: hashes only ever compare
against other hashes produced by this module, so cross-library bit
compatibility doesn't matter — only self-consistency does.

Hamming distance ≤ 10 marks a candidate pair, ≤ 6 a strong one (the
auto-merge tier). Those thresholds live in `engine.py`; this module is
just hash + distance.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

# Same libvips-quieting dance as derivatives.py — must precede the import.
os.environ.setdefault("VIPS_WARNING", "0")
os.environ.setdefault("G_MESSAGES_DEBUG", "")

import logging as _logging

_logging.getLogger("pyvips").setLevel(_logging.WARNING)

HASH_SIDE = 32          # DCT input edge
LOW_FREQ_SIDE = 8       # kept low-frequency block edge → 64-bit hash


def _dct_basis(n: int) -> np.ndarray:
    """Orthonormal DCT-II basis matrix (n×n). `B @ img @ B.T` = 2-D DCT."""
    k = np.arange(n)
    basis = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    basis *= np.sqrt(2.0 / n)
    basis[0] /= np.sqrt(2.0)
    return basis


_BASIS = _dct_basis(HASH_SIDE)


def phash_pixels(gray: np.ndarray) -> int:
    """64-bit pHash of a 32×32 float grayscale array."""
    if gray.shape != (HASH_SIDE, HASH_SIDE):
        raise ValueError(f"expected {HASH_SIDE}x{HASH_SIDE}, got {gray.shape}")
    dct = _BASIS @ gray.astype(np.float64) @ _BASIS.T
    low = dct[:LOW_FREQ_SIDE, :LOW_FREQ_SIDE].ravel()
    median = np.median(low[1:])  # AC only; DC is brightness, not structure
    bits = low > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def phash_file(path: Path) -> int:
    """Decode → orient → 32×32 gray squash (Lanczos) → 64-bit pHash.

    Squashing aspect (Size.FORCE) rather than cropping/padding is
    deliberate: a downscaled export keeps the same aspect, so both copies
    squash identically, while pad/crop would make the hash depend on how
    the padding fell.
    """
    import pyvips  # lazy — heavy import, not needed in pure-unit tests

    image = pyvips.Image.thumbnail(
        str(path),
        HASH_SIDE,
        height=HASH_SIDE,
        size=pyvips.enums.Size.FORCE,
    ).colourspace("b-w")
    gray = np.ndarray(
        buffer=image.write_to_memory(),
        dtype=np.uint8,
        shape=(image.height, image.width, image.bands),
    )[:, :, 0].astype(np.float64)
    return phash_pixels(gray)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_hex(value: int) -> str:
    return f"{value:016x}"


def from_hex(text: str) -> int:
    return int(text, 16)
