"""Audit state + append-only log.

Per-folder:
  .audit/state.yml   — idempotency: applied rule×file×patch-hash
  .audit/audit.jsonl — append-only log of every proposal + action
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


AUDIT_DIR = ".audit"
STATE_FILENAME = "state.yml"
LOG_FILENAME = "audit.jsonl"


@dataclass
class State:
    folder: Path
    applied: dict[str, dict[str, str]] = field(default_factory=dict)
    # structure: applied[file_rel_path][rule_name] = patch_hash

    @classmethod
    def load(cls, folder: Path) -> "State":
        state_file = folder / AUDIT_DIR / STATE_FILENAME
        if not state_file.is_file():
            return cls(folder=folder)
        data = yaml.safe_load(state_file.read_text()) or {}
        return cls(folder=folder, applied=data.get("applied", {}))

    def save(self) -> None:
        target = self.folder / AUDIT_DIR / STATE_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"applied": self.applied}
        target.write_text(yaml.safe_dump(payload, sort_keys=True))

    def is_applied(self, file_rel: str, rule: str, patch_hash: str) -> bool:
        return self.applied.get(file_rel, {}).get(rule) == patch_hash

    def mark_applied(self, file_rel: str, rule: str, patch_hash: str) -> None:
        self.applied.setdefault(file_rel, {})[rule] = patch_hash


def patch_hash(patch: dict[str, Any]) -> str:
    blob = json.dumps(patch, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def log_event(folder: Path, event: dict[str, Any]) -> None:
    target = folder / AUDIT_DIR / LOG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": time.time(), **event}
    with target.open("a") as f:
        f.write(json.dumps(event, default=str) + "\n")
