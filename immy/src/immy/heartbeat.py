"""Per-trip heartbeat file at `.audit/.progress`.

A single tiny YAML written atomically (tmp + rename) whenever the long-
running CLI commands (`process`, `promote`, `audit`) advance to a new
file or phase. External watchers — `tools/promote-all-trips.sh`'s Ctrl+T
handler, a tmux status line, a second-terminal `watch cat` — can poll
this without touching the running process.

Format (intentionally flat so a shell `awk` can parse it too):

    pid: 12345
    started_at: 2026-04-27T18:42:11
    updated_at: 2026-04-27T18:43:09
    phase: process
    step: derivatives
    file: DJI_0123.MP4
    index: 17
    total: 482
    detail: 1280 MB
    elapsed: 58

Why a hidden dotfile in `.audit/` and not e.g. `/tmp`:
- co-located with the trip — moves with the trip on rename, dies on
  `rm -rf .audit/` along with everything else trip-scoped;
- no /tmp permission / namespace games on macOS;
- already excluded from rsync to the NAS (RSYNC_EXCLUDES drops `.audit`).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import AUDIT_DIR


HEARTBEAT_FILENAME = ".progress"


def heartbeat_path(trip_folder: Path) -> Path:
    return trip_folder / AUDIT_DIR / HEARTBEAT_FILENAME


@dataclass
class Heartbeat:
    """Lightweight progress file. Cheap to update — single ~200 B write."""

    path: Path
    phase: str = ""
    pid: int = field(default_factory=os.getpid)
    started_at: float = field(default_factory=time.time)
    _state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_trip(
        cls, trip_folder: Path, phase: str, *, path: Path | None = None,
    ) -> "Heartbeat":
        # `path` redirects the heartbeat off a read-only originals mount (NAS);
        # unset → the default `<trip>/.audit/.progress` (Mac path, unchanged).
        p = path if path is not None else heartbeat_path(trip_folder)
        p.parent.mkdir(parents=True, exist_ok=True)
        hb = cls(path=p, phase=phase)
        hb.write(step="starting")
        return hb

    def write(
        self,
        *,
        step: str | None = None,
        file: str | None = None,
        index: int | None = None,
        total: int | None = None,
        detail: str | None = None,
    ) -> None:
        if step is not None: self._state["step"] = step
        if file is not None: self._state["file"] = file
        if index is not None: self._state["index"] = index
        if total is not None: self._state["total"] = total
        if detail is not None: self._state["detail"] = detail

        now = time.time()
        lines = [
            f"pid: {self.pid}",
            f"started_at: {datetime.fromtimestamp(self.started_at).isoformat(timespec='seconds')}",
            f"updated_at: {datetime.fromtimestamp(now).isoformat(timespec='seconds')}",
            f"phase: {self.phase}",
        ]
        for key in ("step", "file", "index", "total", "detail"):
            if key in self._state:
                lines.append(f"{key}: {self._state[key]}")
        lines.append(f"elapsed: {int(now - self.started_at)}")

        # Atomic: write tmp + rename in same dir. Readers either see the
        # previous good file or the new one — never a half-flushed mix.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "Heartbeat":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.clear()
