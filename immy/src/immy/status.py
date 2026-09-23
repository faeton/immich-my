"""`immy status <trip>` — one screen answering "where is this trip at?".

Reads only what the other commands already leave behind; never writes and
never talks to Immich or Postgres:

- audit      pending HIGH / MEDIUM findings (needs an exiftool pass; skippable)
- process    `y_processed.yml` marker: when, inserted vs already present
- journal    per-worker completion counts (and how many model versions)
- offline    cached entries, synced vs pending
- derivs     staged derivative files the marker names: present vs missing
- heartbeat  last `.progress` write: phase/step, age, whether the pid lives

Paths come from `resolve_writable_paths`, so the NAS layout (state under
`state_root`) resolves the same way `process` wrote it.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

import yaml

from .config import Config
from .journal import Journal
from .paths import WritablePaths, resolve_writable_paths


def _paths(trip: Path, config: Config) -> WritablePaths:
    return resolve_writable_paths(
        trip,
        originals_root=config.originals_root,
        state_root=config.state_root,
        sidecars_root=config.sidecars_root,
    )


def audit_status(trip: Path) -> dict:
    """Pending HIGH / MEDIUM, exactly as `immy audit` would report them."""
    from .cli import _compute_pending
    from .exif import read_folder
    from .state import State

    rows = read_folder(trip)
    _, high, medium, applied = _compute_pending(rows, trip, State.load(trip))
    return {"files": len(rows), "high": len(high), "medium": len(medium), "applied": len(applied)}


def marker_status(paths: WritablePaths) -> dict | None:
    if not paths.marker_path.is_file():
        return None
    data = yaml.safe_load(paths.marker_path.read_text()) or {}
    return {
        "processed_at": data.get("processed_at"),
        "assets": len(data.get("assets") or []),
        "inserted": data.get("inserted", 0),
        "already_present": data.get("already_present", 0),
    }


def journal_status(paths: WritablePaths) -> dict[str, dict]:
    """worker → {done, versions}. More than one version for a worker means a
    model bump left part of the trip on the old model."""
    journal = Journal.load_path(paths.journal_path)
    done: Counter[str] = Counter()
    versions: dict[str, set[str]] = {}
    for workers in journal.entries.values():
        for worker, rec in workers.items():
            done[worker] += 1
            versions.setdefault(worker, set()).add(str(rec.get("version")))
    return {
        w: {"done": done[w], "versions": sorted(versions[w])} for w in sorted(done)
    }


def offline_status(paths: WritablePaths) -> dict:
    from .offline import _load_entry

    total = synced = 0
    if paths.offline_dir.is_dir():
        for yml in paths.offline_dir.glob("*.yml"):
            data = _load_entry(yml)
            if not data:
                continue
            total += 1
            synced += bool(data.get("synced"))
    return {"entries": total, "synced": synced, "pending": total - synced}


def derivatives_status(paths: WritablePaths) -> dict:
    """Staged derivative files named by the marker. `promote` rsyncs them to
    the NAS but leaves the staged copies, so missing ones mean a partial
    `.audit/` wipe — promote would push nothing for those assets."""
    present = missing = 0
    if paths.marker_path.is_file():
        data = yaml.safe_load(paths.marker_path.read_text()) or {}
        for asset in data.get("assets") or []:
            for d in asset.get("derivatives") or []:
                if (paths.derivatives_dir / d["relative_path"]).is_file():
                    present += 1
                else:
                    missing += 1
    return {"present": present, "missing": missing}


def _pid_alive(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except (OSError, ValueError):
        return None
    return True


def heartbeat_status(paths: WritablePaths, *, now: float | None = None) -> dict | None:
    if not paths.heartbeat_path.is_file():
        return None
    data = yaml.safe_load(paths.heartbeat_path.read_text()) or {}
    age = None
    try:
        age = int((now or time.time()) - paths.heartbeat_path.stat().st_mtime)
    except OSError:
        pass
    pid = data.get("pid")
    return {
        "phase": data.get("phase"),
        "step": data.get("step"),
        "file": data.get("file"),
        "index": data.get("index"),
        "total": data.get("total"),
        "age_s": age,
        "pid": pid,
        # Only meaningful when status runs on the host that wrote it.
        "alive": _pid_alive(int(pid)) if pid else None,
    }


def trip_status(trip: Path, config: Config, *, with_audit: bool = True) -> dict:
    paths = _paths(trip, config)
    return {
        "trip": str(trip),
        "audit_dir": str(paths.audit_dir),
        "audit": audit_status(trip) if with_audit else None,
        "process": marker_status(paths),
        "journal": journal_status(paths),
        "offline": offline_status(paths),
        "derivatives": derivatives_status(paths),
        "heartbeat": heartbeat_status(paths),
    }
