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

from immy import captions as captions_mod
from immy import offline as offline_mod
from immy import process as process_mod
from immy.journal import Journal
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


def test_late_caption_unsyncs_and_redrains(tmp_path: Path):
    """The two-command overnight: the sync command may drain a trip before
    the captioner reaches it. A caption written afterwards must flip the
    entry back to unsynced so the next drain re-pushes the description —
    otherwise the overnight caption never reaches Postgres."""
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

    # 1. Sync drains the trip first (caption not computed yet).
    summary1 = offline_mod.sync_trip(target, conn, library=LIB)
    assert summary1["synced"] == 1
    entry_path = next((target / ".audit" / "offline").glob("*.yml"))
    asset_id = yaml.safe_load(entry_path.read_text())["asset"]["id"]
    assert yaml.safe_load(entry_path.read_text())["synced"] is True

    # 2. Captioner lands a description on the already-synced entry. A fresh
    #    process pass reopens the cached entry (repopulating the id→hex map,
    #    as the real captioner run does) before the description write.
    sink2 = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(target, None, LIB, sink=sink2)
    sink2.update_description_if_ai_or_empty(asset_id, "AI: a drone over fields")
    reloaded = yaml.safe_load(entry_path.read_text())
    assert reloaded["synced"] is False, "caption must un-sync the entry"
    assert reloaded["exif"]["description"] == "AI: a drone over fields"

    # 3. Re-drain now re-pushes the entry (caption reaches the DB).
    cur.reset_mock()
    summary2 = offline_mod.sync_trip(target, conn, library=LIB)
    assert summary2["synced"] == 1
    assert summary2["skipped"] == 0


def test_sync_does_not_clobber_concurrent_caption(tmp_path: Path):
    """The snapshot-then-clobber wedge: sync_trip snapshots entries, replays,
    then stamps synced:True. If the parallel captioner rewrites the YAML
    (new description) AFTER the snapshot but BEFORE the stamp, sync must NOT
    write its stale copy back — that would erase the caption forever. The
    re-read-before-stamp guard leaves the captioner's entry (synced:False)
    so the next drain pushes it."""
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    sink = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(target, None, LIB, sink=sink)

    entry_path = next((target / ".audit" / "offline").glob("*.yml"))
    asset_id = yaml.safe_load(entry_path.read_text())["asset"]["id"]

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("replayed-uuid",)
    conn.cursor.return_value = cur

    # Simulate a concurrent captioner: it rewrites the YAML with a fresh
    # description right after sync_trip snapshots, by hooking _replay_entry.
    real_replay = offline_mod._replay_entry

    def _replay_then_caption(c, folder, data, **kw):
        real_replay(c, folder, data, **kw)
        sink2 = offline_mod.OfflineSink(target, LIB)
        process_mod.process_trip(target, None, LIB, sink=sink2)
        sink2.update_description_if_ai_or_empty(asset_id, "AI: late caption")

    import unittest.mock as _mock
    with _mock.patch.object(offline_mod, "_replay_entry", _replay_then_caption):
        offline_mod.sync_trip(target, conn, library=LIB)

    # The on-disk entry must still carry the caption and remain unsynced.
    reloaded = yaml.safe_load(entry_path.read_text())
    assert reloaded["exif"]["description"] == "AI: late caption"
    assert reloaded["synced"] is False, "stale stamp must not clobber the caption"

    # And a follow-up drain pushes it.
    summary = offline_mod.sync_trip(target, conn, library=LIB)
    assert summary["synced"] == 1


# --- Phase 3b parallel caption pool (--caption-workers > 1) ----------------

_CFG = captions_mod.CaptionerConfig(endpoint="http://test.invalid/v1", model="test-vlm")


def _fake_caption_factory(calls: list[str], fail_on: tuple[str, ...] = ()):
    """A stand-in for `captions.caption` that encodes the source filename in
    the text, so a wrong result_idx mapping (caption on the wrong asset)
    fails the assertion. Records every call for concurrency/idempotence
    assertions; raises for names in `fail_on` to exercise the skip path."""
    def _fake(media, *, config, preview=None):
        name = Path(media).name
        calls.append(name)
        if name in fail_on:
            raise captions_mod.CaptionError(f"boom {name}")
        return captions_mod.CaptionResult(
            text=f"desc::{name}", model=config.model,
            prompt_tokens=1, completion_tokens=2,
        )
    return _fake


def _descriptions_by_name(target: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in (target / ".audit" / "offline").glob("*.yml"):
        data = yaml.safe_load(p.read_text())
        name = Path(data["asset"]["original_path"]).name
        out[name] = (data.get("exif") or {}).get("description")
    return out


def test_caption_workers_parallel_records_each_asset(tmp_path: Path, monkeypatch):
    """N>1 fans the VLM calls across a pool yet lands each caption on the
    correct asset (result_idx mapping), persists it to the offline YAML, and
    marks the journal so a resume skips it."""
    target = tmp_path / "clock-drift-simple"
    shutil.copytree(FIXTURES / "clock-drift-simple", target)
    calls: list[str] = []
    monkeypatch.setattr(process_mod.captions_mod, "caption",
                        _fake_caption_factory(calls))

    sink = offline_mod.OfflineSink(target, LIB)
    results = process_mod.process_trip(
        target, None, LIB, sink=sink,
        compute_captions=True, captioner_config=_CFG, caption_workers=3,
    )

    imgs = {Path(r.container_path).name: r for r in results
            if Path(r.container_path).name.endswith(".JPG")}
    assert len(imgs) == 4
    # Each result carries ITS OWN caption (mapping is correct, not crossed).
    for name, r in imgs.items():
        assert r.caption is not None and r.caption["text"] == f"desc::{name}"
    # Every eligible image hit the VLM exactly once, in parallel.
    assert sorted(calls) == sorted(imgs)
    # Persisted to the offline cache with the AI: prefix.
    descs = _descriptions_by_name(target)
    for name in imgs:
        assert descs[name] == f"AI: desc::{name}"

    # Resume: a second pass over the same trip must skip every caption
    # (journal marked done) — no new VLM calls.
    calls.clear()
    sink2 = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(
        target, None, LIB, sink=sink2,
        compute_captions=True, captioner_config=_CFG, caption_workers=3,
    )
    assert calls == [], "captions already journaled must not re-run"


def test_caption_workers_parallel_matches_sequential(tmp_path: Path, monkeypatch):
    """The parallel pool (N=3) produces the same per-asset descriptions as
    the untouched sequential path (N=1)."""
    def _run(workers: int) -> dict[str, str]:
        target = tmp_path / f"trip-{workers}"
        shutil.copytree(FIXTURES / "clock-drift-simple", target)
        monkeypatch.setattr(process_mod.captions_mod, "caption",
                            _fake_caption_factory([]))
        sink = offline_mod.OfflineSink(target, LIB)
        process_mod.process_trip(
            target, None, LIB, sink=sink,
            compute_captions=True, captioner_config=_CFG, caption_workers=workers,
        )
        return _descriptions_by_name(target)

    assert _run(1) == _run(3)


def test_caption_workers_pool_isolates_failures(tmp_path: Path, monkeypatch):
    """A VLM error on one asset (on_caption_error='skip', the default) must
    not abort the pool: the other assets still get captioned, and the failed
    one is left without a caption (regenerated on a later run)."""
    target = tmp_path / "clock-drift-simple"
    shutil.copytree(FIXTURES / "clock-drift-simple", target)
    calls: list[str] = []
    monkeypatch.setattr(process_mod.captions_mod, "caption",
                        _fake_caption_factory(calls, fail_on=("DSC_0002.JPG",)))

    sink = offline_mod.OfflineSink(target, LIB)
    results = process_mod.process_trip(
        target, None, LIB, sink=sink,
        compute_captions=True, captioner_config=_CFG, caption_workers=3,
    )

    by_name = {Path(r.container_path).name: r for r in results}
    assert by_name["DSC_0002.JPG"].caption is None       # failed → no caption
    for name in ("DSC_0001.JPG", "DSC_0003.JPG", "DSC_0004.JPG"):
        assert by_name[name].caption["text"] == f"desc::{name}"
    # The failed asset has no journal caption entry, so a rerun retries it.
    j = Journal.load(target)
    caption_workers_done = sum(
        1 for rec in j.entries.values() if "caption" in rec
    )
    assert caption_workers_done == 3
    # And on disk: the failed asset's entry has no description and no caption
    # block — nothing was half-written for it.
    for p in (target / ".audit" / "offline").glob("*.yml"):
        data = yaml.safe_load(p.read_text())
        if Path(data["asset"]["original_path"]).name == "DSC_0002.JPG":
            assert not (data.get("exif") or {}).get("description")
            assert "caption" not in data


def test_caption_workers_rechecks_user_text_before_recording(tmp_path: Path, monkeypatch):
    """The TOCTOU guard: a non-AI description (hand edit / Whisper / concurrent
    sync) that lands AFTER the sequential-pass enqueue but before the pool
    records must NOT be journaled or counted as an AI caption — matching the
    sequential path, which re-reads `get_description` right before its call.
    We simulate that by flipping `get_description` to return user text once the
    pool starts: phase 1 sees empty (enqueues + calls the VLM), phase 3 sees
    the user text and skips recording."""
    target = tmp_path / "clock-drift-simple"
    shutil.copytree(FIXTURES / "clock-drift-simple", target)

    sink = offline_mod.OfflineSink(target, LIB)
    real_get = sink.get_description
    state = {"in_pool": False}
    calls: list[str] = []

    def _fake_get(asset_id: str):
        # Once the pool is running, pretend a user description appeared.
        return "hand-typed note" if state["in_pool"] else real_get(asset_id)

    def _fake_caption(media, *, config, preview=None):
        state["in_pool"] = True  # we're now past the sequential pass
        calls.append(Path(media).name)
        return captions_mod.CaptionResult(
            text=f"desc::{Path(media).name}", model=config.model,
            prompt_tokens=1, completion_tokens=2,
        )

    monkeypatch.setattr(sink, "get_description", _fake_get)
    monkeypatch.setattr(process_mod.captions_mod, "caption", _fake_caption)

    results = process_mod.process_trip(
        target, None, LIB, sink=sink,
        compute_captions=True, captioner_config=_CFG, caption_workers=3,
    )

    imgs = [Path(r.container_path).name for r in results
            if Path(r.container_path).name.endswith(".JPG")]
    # The VLM ran for every image (phase-1 guard saw empty descriptions)...
    assert sorted(calls) == sorted(imgs)
    # ...but the phase-3 re-check saw the user text and recorded NONE of them.
    assert all(r.caption is None for r in results
               if Path(r.container_path).name.endswith(".JPG"))
    j = Journal.load(target)
    assert sum(1 for rec in j.entries.values() if "caption" in rec) == 0


def test_caption_workers_ticks_heartbeat(tmp_path: Path, monkeypatch):
    """The pool must refresh the per-trip heartbeat on every completion, else
    its age climbs past the overnight dashboard's 120s 'stuck?' threshold even
    while captions stream in. Also confirms the progress bar tracks captions
    (index/total = jobs), not the prior derivatives scan count."""
    from immy.heartbeat import Heartbeat

    target = tmp_path / "clock-drift-simple"
    shutil.copytree(FIXTURES / "clock-drift-simple", target)
    monkeypatch.setattr(process_mod.captions_mod, "caption",
                        _fake_caption_factory([]))

    ticks: list[tuple] = []
    real_write = Heartbeat.write

    def _spy(self, **kw):
        if kw.get("step") == "caption" and "index" in kw:
            ticks.append((kw.get("index"), kw.get("total")))
        return real_write(self, **kw)

    monkeypatch.setattr(Heartbeat, "write", _spy)

    sink = offline_mod.OfflineSink(target, LIB)
    process_mod.process_trip(
        target, None, LIB, sink=sink,
        compute_captions=True, captioner_config=_CFG, caption_workers=2,
    )

    # Initial tick at 0/4, then one per completion up to 4/4; total stays 4.
    assert (0, 4) in ticks
    assert max(i for i, _ in ticks) == 4
    assert all(t == 4 for _, t in ticks)


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
