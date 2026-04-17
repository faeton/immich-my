"""Insta360 .insv ↔ .lrv pairing by shared timestamp+serial.

This rule records the pairing in state only. The actual Immich stack API
call happens in `immy promote` (iteration 2a.4).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..exif import ExifRow
from ..filenames import parse_insta360
from .registry import Finding, Rule, register


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    buckets: dict[tuple[str, str], dict[str, Path]] = defaultdict(dict)

    for row in rows:
        ext = row.path.suffix.lower()
        if ext not in (".insv", ".lrv"):
            continue
        key = parse_insta360(row.path)
        if key is None:
            continue
        kind = "insv" if ext == ".insv" else "lrv"
        buckets[(key.timestamp, key.serial)][kind] = row.path

    for (ts, serial), pair in buckets.items():
        if "insv" in pair and "lrv" in pair:
            # One finding per file, cross-referenced.
            for side, other_side in (("insv", "lrv"), ("lrv", "insv")):
                out.append(Finding(
                    rule="insta360-pair-by-ts-serial",
                    confidence="high",
                    path=pair[side],
                    action="pair",
                    pair_with=pair[other_side],
                    reason=f"shared timestamp {ts} + serial {serial}",
                ))
    return out


register(Rule(
    name="insta360-pair-by-ts-serial",
    confidence="high",
    propose=_propose,
))
