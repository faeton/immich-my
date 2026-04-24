"""Per-trip phase journal for resumable enrichment workers.

Stored at `<trip>/.audit/journal.yml` as a single mapping
keyed by `checksum_hex → {worker → {version, completed_at, meta?}}`.

Why a per-trip journal (not a Postgres table):
- Works in offline mode — no DB required to know what's done.
- Co-located with derivatives + offline cache, so deleting `.audit/`
  resets a trip cleanly.
- Cheap: a few KB even for 1000 assets, single fsync per phase.

Why `(checksum, worker, version)`:
- `checksum` is the stable identity (sha1-path), already used for
  asset rows. Same key the offline cache uses.
- `worker` is the phase name (`derivatives`, `clip`, `faces`,
  `transcript`, `caption`).
- `version` lets a model bump invalidate just that worker's entries.
  e.g. switching the captioner from Gemma-3-4B to Gemma-3-27B writes
  a new version string; the journal reports "not done at v=27B" and
  the work re-runs.

Atomic writes: we serialize through a tmp file + rename so a crash
mid-flush leaves the previous good state intact, never a half-written
YAML.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .state import AUDIT_DIR


JOURNAL_FILENAME = "journal.yml"


def journal_path(trip_folder: Path) -> Path:
    return trip_folder / AUDIT_DIR / JOURNAL_FILENAME


@dataclass
class Journal:
    """In-memory view of `.audit/journal.yml` with atomic flush."""

    path: Path
    entries: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # `dirty` is set whenever `mark_done` / `clear_worker` mutates state,
    # so `flush()` can skip the multi-MB YAML rewrite on trips where the
    # current run touched nothing (fully-cached resume scans). Without
    # this, a 3k-asset trip paid ~1–2 s per file rewriting the journal
    # even when no phase ran.
    _dirty: bool = False

    @classmethod
    def load(cls, trip_folder: Path) -> "Journal":
        p = journal_path(trip_folder)
        if not p.is_file():
            return cls(path=p)
        data = yaml.safe_load(p.read_text()) or {}
        # Defensive: drop anything that doesn't match the expected shape
        # rather than raising. A malformed entry just re-runs its phase.
        entries: dict[str, dict[str, dict[str, Any]]] = {}
        for cs, workers in (data.get("entries") or {}).items():
            if not isinstance(workers, dict):
                continue
            clean: dict[str, dict[str, Any]] = {}
            for w, rec in workers.items():
                if isinstance(rec, dict) and "version" in rec:
                    clean[str(w)] = dict(rec)
            if clean:
                entries[str(cs)] = clean
        return cls(path=p, entries=entries)

    def is_done(self, checksum_hex: str, worker: str, version: str) -> bool:
        rec = self.entries.get(checksum_hex, {}).get(worker)
        return bool(rec and rec.get("version") == version)

    def get(self, checksum_hex: str, worker: str) -> dict[str, Any] | None:
        rec = self.entries.get(checksum_hex, {}).get(worker)
        return dict(rec) if rec else None

    def mark_done(
        self,
        checksum_hex: str,
        worker: str,
        version: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        rec: dict[str, Any] = {"version": version, "completed_at": int(time.time())}
        if meta:
            rec["meta"] = dict(meta)
        self.entries.setdefault(checksum_hex, {})[worker] = rec
        self._dirty = True

    def clear_worker(self, checksum_hex: str, worker: str) -> None:
        if checksum_hex in self.entries:
            removed = self.entries[checksum_hex].pop(worker, None)
            if not self.entries[checksum_hex]:
                self.entries.pop(checksum_hex)
            if removed is not None:
                self._dirty = True

    def flush(self) -> None:
        """Atomic write: tmp file in the same dir, then rename. Same dir
        is required for rename to be atomic on POSIX (must stay within one
        filesystem). No-op when nothing has changed since the last flush —
        resumed trips where every asset is cached skip the YAML rewrite
        entirely."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "entries": self.entries}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(payload, sort_keys=True))
        os.replace(tmp, self.path)
        self._dirty = False


# --- Worker version strings ----------------------------------------------
#
# Versions are short opaque strings: model id, schema rev, or a constant.
# A change here invalidates prior journal entries for that worker, which
# is exactly what we want when bumping a model.

DERIVATIVES_VERSION = "v1"  # bump on layout/format changes


def clip_version(model: str) -> str:
    return f"clip:{model}"


def faces_version(model: str) -> str:
    return f"faces:{model}"


def transcript_version(model: str) -> str:
    return f"whisper:{model}"


def caption_version(model: str) -> str:
    return f"caption:{model}"


__all__ = [
    "Journal",
    "JOURNAL_FILENAME",
    "journal_path",
    "DERIVATIVES_VERSION",
    "clip_version",
    "faces_version",
    "transcript_version",
    "caption_version",
]
