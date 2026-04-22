"""Offline-mode cache format + replay parity with online path.

These tests don't hit a real database. They exercise:

- `OfflineSink` writes one YAML per asset keyed by path-checksum hex,
  with placeholder substitution for owner_id / library_id when the DB
  was unreachable at cache time.
- `sync_trip` replays entries by emitting the same SQL statements the
  online path would (INSERT asset / exif / smart_search / asset_face),
  substituting placeholders from the live LibraryInfo at sync time.
- Re-running `process_trip` with an OfflineSink on an already-cached
  trip is a no-op (entries keep the same asset UUIDs).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from immy import offline as offline_mod
from immy import process as process_mod
from immy.pg import LibraryInfo


FIXTURES = Path(__file__).parent / "fixtures"

LIB = LibraryInfo(
    id="lib-abc",
    owner_id="owner-xyz",
    container_root="/mnt/external/originals",
)


def test_offline_sink_writes_entry_per_asset(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    sink = offline_mod.OfflineSink(target, LIB)

    results = process_mod.process_trip(target, None, LIB, sink=sink)

    assert len(results) == 1
    entries = list((target / ".audit" / "offline").glob("*.yml"))
    assert len(entries) == 1
    data = yaml.safe_load(entries[0].read_text())
    assert data["synced"] is False
    assert data["owner_id"] == "owner-xyz"
    assert data["library_id"] == "lib-abc"
    assert data["asset"]["original_path"].endswith("/DJI_0001.JPG")


def test_offline_rerun_reuses_asset_id(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    sink1 = offline_mod.OfflineSink(target, LIB)
    r1 = process_mod.process_trip(target, None, LIB, sink=sink1)
    assert r1[0].inserted is True
    first_id = r1[0].asset_id

    # Second pass: same sink class, fresh instance. Must find the
    # existing YAML, reuse the UUID, and report inserted=False.
    sink2 = offline_mod.OfflineSink(target, LIB)
    r2 = process_mod.process_trip(target, None, LIB, sink=sink2)
    assert r2[0].inserted is False
    assert r2[0].asset_id == first_id


def test_placeholder_substitution_on_sync(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    placeholder_lib = LibraryInfo(
        id="__offline_placeholder__",
        owner_id="__offline_placeholder__",
        container_root="/mnt/external/originals",
    )
    sink = offline_mod.OfflineSink(target, placeholder_lib)
    process_mod.process_trip(target, None, placeholder_lib, sink=sink)

    # Confirm cached values are placeholders.
    entries = list((target / ".audit" / "offline").glob("*.yml"))
    data = yaml.safe_load(entries[0].read_text())
    assert data["asset"]["owner_id"] == "__offline_placeholder__"

    # Replay through a fake conn and assert the INSERT carries real
    # values from the resolved-at-sync-time LibraryInfo.
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("replayed-uuid",)
    conn.cursor.return_value = cur

    offline_mod.sync_trip(target, conn, library=LIB)

    # First execute is INSERT INTO asset with real owner_id / library_id.
    insert_call = next(
        c for c in cur.execute.call_args_list
        if "INSERT INTO asset" in c.args[0]
    )
    _, params = insert_call.args
    assert params["owner_id"] == "owner-xyz"
    assert params["library_id"] == "lib-abc"


def test_sync_marks_entries_synced_and_skips_on_rerun(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    sink = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(target, None, LIB, sink=sink)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("replayed-uuid",)
    conn.cursor.return_value = cur

    summary1 = offline_mod.sync_trip(target, conn, library=LIB)
    assert summary1["synced"] == 1
    assert summary1["skipped"] == 0

    # Second sync: entries already marked synced → skipped, no DB hit.
    cur.reset_mock()
    summary2 = offline_mod.sync_trip(target, conn, library=LIB)
    assert summary2["synced"] == 0
    assert summary2["skipped"] == 1
    cur.execute.assert_not_called()


def test_derive_container_root_from_marker(tmp_path: Path):
    trip = tmp_path / "2024-02-chile"
    (trip / ".audit").mkdir(parents=True)
    marker = trip / ".audit" / "y_processed.yml"
    marker.write_text(yaml.safe_dump({
        "assets": [
            {"file": "/mnt/external/originals/2024-02-chile/foo.jpg"},
        ],
    }))
    assert offline_mod.derive_container_root_from_marker(trip) == (
        "/mnt/external/originals"
    )


def test_offline_resume_skips_captioned_files(tmp_path: Path, monkeypatch):
    """Per-file resumability: if the YAML entry already has a caption
    from the same model, `immy process --offline` on a re-run must not
    call the VLM again. This is the overnight-run-Ctrl-C-and-resume case."""
    from immy import captions as captions_mod

    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    config = captions_mod.CaptionerConfig(
        endpoint="http://x", model="gemma-mock",
    )

    calls = {"n": 0}

    def fake_caption(media, *, config, preview=None):
        calls["n"] += 1
        return captions_mod.CaptionResult(
            text="a cat on a roof", model=config.model,
            prompt_tokens=10, completion_tokens=5,
        )

    monkeypatch.setattr(captions_mod, "caption", fake_caption)

    sink1 = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(
        target, None, LIB, sink=sink1,
        compute_captions=True, captioner_config=config,
    )
    assert calls["n"] == 1

    # Second pass with a fresh sink — must read the cached caption and
    # skip the VLM entirely.
    sink2 = offline_mod.OfflineSink(target, LIB)
    results = process_mod.process_trip(
        target, None, LIB, sink=sink2,
        compute_captions=True, captioner_config=config,
    )
    assert calls["n"] == 1, "VLM must not be called on a re-run"
    assert results[0].caption["model"] == "gemma-mock"

    # Same YAML but *different* model id → upgrade path: VLM runs again.
    upgrade = captions_mod.CaptionerConfig(
        endpoint="http://x", model="gemma-v2-mock",
    )
    sink3 = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(
        target, None, LIB, sink=sink3,
        compute_captions=True, captioner_config=upgrade,
    )
    assert calls["n"] == 2


def test_cache_library_info_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(offline_mod, "LIBRARY_CACHE_PATH", tmp_path / "lib.yml")
    offline_mod.cache_library_info(LIB)
    loaded = offline_mod.load_cached_library()
    assert loaded == LIB
