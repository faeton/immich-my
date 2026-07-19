"""Executor compress path: swap discipline, no-gain, failure isolation,
crash-heal. Encoding is faked — these tests never run ffmpeg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from immy.dedup import manifest
from immy.triage import executor


def _seed(conn, id: int, path: str, bytes: int) -> None:
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, media_type, format)"
        " VALUES (?, 'originals', ?, 'canonical', ?, 'video', 'mp4')",
        (id, path, bytes),
    )
    conn.execute(
        "INSERT INTO triage (asset_id, verdict, decided_by, decided_at)"
        " VALUES (?, 'compress', 'human', '2026-07-19T00:00:00')",
        (id,),
    )


@pytest.fixture
def env(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    fs = tmp_path / "originals" / "2024-04-namibia"
    fs.mkdir(parents=True)
    (fs / "DJI_0001.MP4").write_bytes(b"x" * 1000)
    (fs / "DJI_0002.MP4").write_bytes(b"x" * 1000)
    (fs / "DJI_0003.MP4").write_bytes(b"x" * 1000)
    _seed(conn, 1, "/originals/2024-04-namibia/DJI_0001.MP4", 1000)
    _seed(conn, 2, "/originals/2024-04-namibia/DJI_0002.MP4", 1000)
    _seed(conn, 3, "/originals/2024-04-namibia/DJI_0003.MP4", 1000)
    conn.commit()
    return conn, tmp_path


def _run(conn, tmp_path, encode_fn, **kw):
    return executor.apply_compress(
        conn, root="/originals", fs_root=str(tmp_path / "originals"),
        quarantine_root=tmp_path / "q", scratch=tmp_path / "scratch",
        dry_run=False, encode_fn=encode_fn,
        duration_fn=lambda p: 10.0, **kw,
    )


def test_swap_quarantine_and_stamp(env, tmp_path):
    conn, _ = env

    def encode(src, dst, threads):
        dst.write_bytes(b"c" * 400)   # 60% smaller

    res = _run(conn, tmp_path, encode)
    assert (res.swapped, res.failed, res.no_gain) == (3, 0, 0)
    swapped = tmp_path / "originals" / "2024-04-namibia" / "DJI_0001.MP4"
    assert swapped.read_bytes() == b"c" * 400
    assert (tmp_path / "q" / "2024-04-namibia" / "DJI_0001.MP4").read_bytes() == b"x" * 1000
    row = conn.execute(
        "SELECT applied_at IS NOT NULL, bytes FROM triage t JOIN asset a"
        " ON a.id=t.asset_id WHERE t.asset_id=1"
    ).fetchone()
    assert row == (1, 400)
    # nothing left pending → second run is a no-op
    res2 = _run(conn, tmp_path, encode)
    assert res2.processed == 0


def test_no_gain_keeps_original(env, tmp_path):
    conn, _ = env

    def encode(src, dst, threads):
        dst.write_bytes(b"c" * 990)   # barely smaller → not worth it

    res = _run(conn, tmp_path, encode, limit=1)
    assert (res.swapped, res.no_gain) == (0, 1)
    original = tmp_path / "originals" / "2024-04-namibia" / "DJI_0001.MP4"
    assert original.read_bytes() == b"x" * 1000          # untouched
    applied, reason = conn.execute(
        "SELECT applied_at IS NOT NULL, reason FROM triage WHERE asset_id=1"
    ).fetchone()
    assert applied == 1 and "no-gain" in reason           # never retried


def test_failure_isolated_and_logged(env, tmp_path):
    conn, _ = env
    calls = []

    def encode(src, dst, threads):
        calls.append(src.name)
        if src.name == "DJI_0001.MP4":
            raise RuntimeError("boom")
        dst.write_bytes(b"c" * 400)

    res = _run(conn, tmp_path, encode)
    assert (res.swapped, res.failed) == (2, 1)
    assert len(calls) == 3                                # one failure didn't stop the run
    status = conn.execute(
        "SELECT status FROM exec_log WHERE asset_id=1"
    ).fetchone()[0]
    assert status == "failed"
    assert conn.execute(
        "SELECT applied_at FROM triage WHERE asset_id=1"
    ).fetchone()[0] is None                               # retried next run


def test_duration_mismatch_fails_encode(env, tmp_path):
    conn, _ = env

    def encode(src, dst, threads):
        dst.write_bytes(b"c" * 400)

    res = executor.apply_compress(
        conn, root="/originals", fs_root=str(tmp_path / "originals"),
        quarantine_root=tmp_path / "q", scratch=tmp_path / "scratch",
        dry_run=False, encode_fn=encode, limit=1,
        duration_fn=lambda p: 10.0 if "originals" in str(p) else 3.0,
    )
    assert res.failed == 1 and res.swapped == 0


def test_heal_finishes_interrupted_swap(tmp_path):
    fs = tmp_path / "t"
    fs.mkdir()
    (fs / f"{executor.NEW_PREFIX}a.mp4").write_bytes(b"new")      # target missing
    (fs / f"{executor.NEW_PREFIX}b.mp4").write_bytes(b"stale")
    (fs / "b.mp4").write_bytes(b"done")                           # swap completed
    healed = executor.heal(tmp_path, log=lambda s: None)
    assert healed == 1
    assert (fs / "a.mp4").read_bytes() == b"new"
    assert (fs / "b.mp4").read_bytes() == b"done"
    assert not (fs / f"{executor.NEW_PREFIX}b.mp4").exists()


def test_dry_run_touches_nothing(env, tmp_path):
    conn, _ = env
    res = executor.apply_compress(
        conn, root="/originals", fs_root=str(tmp_path / "originals"),
        quarantine_root=tmp_path / "q", scratch=tmp_path / "scratch",
        dry_run=True, encode_fn=lambda *a: (_ for _ in ()).throw(AssertionError),
        duration_fn=lambda p: 10.0,
    )
    assert res.processed == 3 and res.bytes_in == 3000
    assert conn.execute(
        "SELECT COUNT(*) FROM triage WHERE applied_at IS NOT NULL"
    ).fetchone()[0] == 0
