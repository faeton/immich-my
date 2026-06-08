"""Rules engine.

Each rule sees the full set of `ExifRow`s plus the folder root and returns
a list of `Finding`s. A Finding describes a patch to apply to a single
file's XMP sidecar (or a non-write action like "pair with X").
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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


def dedup_by_field(findings: list[Finding]) -> list[Finding]:
    """Per-tier, per-(path, xmp_field) dedup. Within a confidence tier the
    first-registered rule wins (specific > general). Tiers are independent
    so a MEDIUM finding still surfaces when a HIGH rule claims the same
    field — the user decides whether MEDIUM overrides after HIGH lands.

    Shared by `immy audit` (which surfaces/applies pending findings) and
    `immy promote` (which gates on pending HIGH). They MUST dedup the same
    way: when two HIGH rules claim a file's GPS — e.g. `dji-gps-from-srt`
    (already applied) and `trip-gps-from-siblings` (the loser) — only the
    winner counts. If promote counted the deduped-out loser as "pending"
    it would refuse to promote a folder audit considers clean, stranding
    the trip forever.
    """
    out: list[Finding] = []
    for tier in ("high", "medium", "low"):
        claimed: set[tuple] = set()
        for f in findings:
            if f.confidence != tier:
                continue
            if f.action != "write_xmp":
                out.append(f)
                continue
            remaining = {k: v for k, v in f.patch.items() if (f.path, k) not in claimed}
            if not remaining:
                continue
            for k in remaining:
                claimed.add((f.path, k))
            out.append(f if remaining == f.patch else replace(f, patch=remaining))
    return out
