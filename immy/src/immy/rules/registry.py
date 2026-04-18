"""Rules engine.

Each rule sees the full set of `ExifRow`s plus the folder root and returns
a list of `Finding`s. A Finding describes a patch to apply to a single
file's XMP sidecar (or a non-write action like "pair with X").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from ..exif import ExifRow


Confidence = Literal["high", "medium", "low"]
Action = Literal["write_xmp", "write_notes", "pair", "note"]


@dataclass
class Finding:
    rule: str
    confidence: Confidence
    path: Path
    action: Action
    patch: dict[str, str] = field(default_factory=dict)
    pair_with: Path | None = None
    reason: str = ""
    # Batch key: findings sharing a non-empty `group` are collapsed into a
    # single y/n prompt (e.g. "Sony A7 is -3h vs Nikon Z50 — apply to 42
    # file(s)?") instead of asking per-file. Empty string = per-file prompt.
    group: str = ""


Propose = Callable[[list[ExifRow], Path], list[Finding]]


@dataclass
class Rule:
    name: str
    confidence: Confidence
    propose: Propose


registry: list[Rule] = []


def register(rule: Rule) -> Rule:
    registry.append(rule)
    return rule


def evaluate(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for rule in registry:
        out.extend(rule.propose(rows, folder))
    return out
