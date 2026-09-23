"""`immy status <trip>` — read-only summary of a trip's leftover state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from immy import status
from immy.cli import app
from immy.config import Config
from immy.journal import Journal
from immy.paths import resolve_writable_paths

runner = CliRunner()


def _config(**kw) -> Config:
    base = dict(originals_root=None, immich=None, pg=None, media=None, ml=None,
                notes_filename=None, source=None)
    return Config(**{**base, **kw})


def _seed(trip: Path, paths) -> None:
    paths.audit_dir.mkdir(parents=True)
    staged = paths.derivatives_dir / "thumbs/u/aa/bb/a1_thumbnail.webp"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"x")
    paths.marker_path.write_text(yaml.safe_dump({
        "processed_at": 1_780_000_000, "inserted": 1, "already_present": 1,
        "assets": [
            {"file": "/o/a.jpg", "id": "a1", "derivatives": [
                {"kind": "thumbnail", "relative_path": "thumbs/u/aa/bb/a1_thumbnail.webp"},
                {"kind": "preview", "relative_path": "thumbs/u/aa/bb/a1_preview.jpeg"},
            ]},
            {"file": "/o/b.jpg", "id": "b2"},
        ],
    }))
    journal = Journal.load_path(paths.journal_path)
    journal.mark_done("c1", "clip", "v1")
    journal.mark_done("c2", "clip", "v2")
    journal.mark_done("c1", "derivatives", "v1")
    journal.flush()
    paths.offline_dir.mkdir()
    (paths.offline_dir / "c1.yml").write_text(yaml.safe_dump({"synced": True, "asset": {}}))
    (paths.offline_dir / "c2.yml").write_text(yaml.safe_dump({"synced": False, "asset": {}}))
    paths.heartbeat_path.write_text(yaml.safe_dump({
        "pid": os.getpid(), "phase": "process", "step": "clip", "index": 3, "total": 9,
    }))


def test_trip_status_reads_the_nas_state_root(tmp_path):
    originals, state = tmp_path / "originals", tmp_path / "state"
    trip = originals / "2024-06-trip"
    trip.mkdir(parents=True)
    cfg = _config(originals_root=originals, state_root=state)
    paths = resolve_writable_paths(trip, originals_root=originals, state_root=state)
    _seed(trip, paths)

    info = status.trip_status(trip, cfg, with_audit=False)

    assert info["audit_dir"] == str(state / "2024-06-trip" / ".audit")
    assert info["process"] == {"processed_at": 1_780_000_000, "assets": 2,
                               "inserted": 1, "already_present": 1}
    assert info["journal"] == {
        "clip": {"done": 2, "versions": ["v1", "v2"]},
        "derivatives": {"done": 1, "versions": ["v1"]},
    }
    assert info["offline"] == {"entries": 2, "synced": 1, "pending": 1}
    assert info["derivatives"] == {"present": 1, "missing": 1}
    assert info["heartbeat"]["alive"] is True
    assert info["heartbeat"]["step"] == "clip"


def test_unprocessed_trip_is_all_empty(tmp_path):
    info = status.trip_status(tmp_path, _config(), with_audit=False)
    assert info["process"] is None and info["heartbeat"] is None
    assert info["journal"] == {}
    assert info["offline"] == {"entries": 0, "synced": 0, "pending": 0}


def test_dead_pid_reads_as_exited(tmp_path):
    paths = resolve_writable_paths(tmp_path)
    paths.audit_dir.mkdir()
    paths.heartbeat_path.write_text(yaml.safe_dump({"pid": 2**22 + 12345, "phase": "p"}))
    assert status.heartbeat_status(paths)["alive"] is False


def test_cli_json(tmp_path):
    result = runner.invoke(app, ["status", str(tmp_path), "--no-audit", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["process"] is None


def test_cli_audit_counts_pending(tmp_path):
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "0 HIGH, 0 MEDIUM pending" in " ".join(result.stdout.split())
