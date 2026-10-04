"""Tests for `immy photos pull` — completeness, pruning, delivery, retry ledger.

osxphotos, ssh and rsync are never run: the report is written by hand in the
shape osxphotos 0.77.2 produces (checked against a real 3-asset export), and
the transport goes through a fake runner that acts on a local "remote" dir.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from immy import photos_pull as pp
from immy.dedup.photos import REPORT_NAME


def _rec(path: Path, uuid: str, **flags) -> dict:
    base = {"filename": str(path), "exported": True, "skipped": False,
            "missing": False, "error": "", "uuid": uuid,
            "sidecar_xmp": False, "sidecar_json": False}
    base.update(flags)
    return base


def _touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# --- completeness ---------------------------------------------------------


def test_live_photo_needs_its_video(tmp_path) -> None:
    heic = _touch(tmp_path / "IMG_1.HEIC")
    mov = _touch(tmp_path / "IMG_1.mov")
    lone = _touch(tmp_path / "IMG_2.HEIC")
    records = [_rec(heic, "L1"), _rec(mov, "L1"), _rec(lone, "L2")]
    check = pp.check_batch(records, ["L1", "L2"], live={"L1", "L2"})
    assert set(check.complete) == {"L1"}
    assert check.incomplete == {"L2": "live photo without its video"}


def test_sidecar_alone_is_not_an_export(tmp_path) -> None:
    xmp = _touch(tmp_path / "a.HEIC.xmp")
    records = [_rec(xmp, "U1", sidecar_xmp=True)]
    check = pp.check_batch(records, ["U1"], live=set())
    assert check.incomplete == {"U1": "nothing exported"}


def test_error_missing_and_absent_uuids_are_incomplete(tmp_path) -> None:
    a = _touch(tmp_path / "a.jpg")
    empty = _touch(tmp_path / "b.jpg", b"")
    records = [
        _rec(a, "ERR", error="download failed"),
        _rec(tmp_path / "c.jpg", "MISS", exported=False, missing=True),
        _rec(empty, "EMPTY"),
    ]
    check = pp.check_batch(records, ["ERR", "MISS", "EMPTY", "GONE"], live=set())
    assert check.complete == {}
    assert check.incomplete["ERR"] == "download failed"
    assert check.incomplete["MISS"] == "missing in Photos library"
    assert check.incomplete["EMPTY"] == "nothing exported"
    assert check.incomplete["GONE"] == "nothing exported"


def test_prune_removes_only_incomplete_files_and_records(tmp_path) -> None:
    batch = tmp_path / "b"
    good = _touch(batch / "2026/09/good.jpg")
    good_xmp = _touch(batch / "2026/09/good.jpg.xmp")
    bad = _touch(batch / "2026/09/bad.HEIC")
    bad_xmp = _touch(batch / "2026/09/bad.HEIC.xmp")
    records = [_rec(good, "G"), _rec(good_xmp, "G", sidecar_xmp=True),
               _rec(bad, "B"), _rec(bad_xmp, "B", sidecar_xmp=True)]
    check = pp.check_batch(records, ["G", "B"], live={"B"})
    pp.prune_incomplete(batch, records, check)
    assert good.exists() and good_xmp.exists()
    assert not bad.exists() and not bad_xmp.exists()
    kept = json.loads((batch / REPORT_NAME).read_text())
    assert {r["uuid"] for r in kept} == {"G"}


# --- ledger ---------------------------------------------------------------


def test_enqueue_is_idempotent_and_retries_come_after_pending(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    assert pp.enqueue(conn, ["A", "B", "C"]) == 3
    assert pp.enqueue(conn, ["A", "D"]) == 1
    conn.execute("UPDATE uuid_state SET status=?, attempts=1 WHERE uuid='A'", (pp.FAILED,))
    conn.execute("UPDATE uuid_state SET status=?, attempts=? WHERE uuid='B'",
                 (pp.FAILED, pp.MAX_ATTEMPTS))
    conn.execute("UPDATE uuid_state SET status=? WHERE uuid='C'", (pp.DELIVERED,))
    assert pp.next_uuids(conn, 10) == ["D", "A"]   # B gave up, C done


# --- export_batch with a fake osxphotos -----------------------------------


def _photos_db(live: set[str], created: dict[str, float] | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ZASSET (ZUUID TEXT, ZKIND INTEGER, ZKINDSUBTYPE INTEGER, "
                 "ZDATECREATED REAL)")
    for u in live | set(created or {}):
        conn.execute("INSERT INTO ZASSET VALUES (?, 0, ?, ?)",
                     (u, 2 if u in live else 0, (created or {}).get(u)))
    return conn


def _fake_osxphotos(exports: dict[str, list[str]], missing: dict[str, list[str]] | None = None,
                    returncode: int = 0):
    """Runner that 'exports' the given relpaths per UUID and writes a report;
    `missing` relpaths get a `missing: true` record and no file (PhotoKit's
    answer for an iCloud-only Live video)."""
    def run(cmd):
        assert cmd[1] == "export"
        batch_dir = Path(cmd[2])
        report = Path(cmd[cmd.index("--report") + 1])
        wanted = Path(cmd[cmd.index("--uuid-from-file") + 1]).read_text().split()
        (batch_dir / ".osxphotos_export.db").write_bytes(b"db")
        recs = []
        for u in wanted:
            for rel in exports.get(u, []):
                p = _touch(batch_dir / rel)
                recs.append(_rec(p, u, sidecar_xmp=rel.endswith(".xmp"),
                                 sidecar_json=rel.endswith(".json")))
            for rel in (missing or {}).get(u, []):
                recs.append(_rec(batch_dir / rel, u, exported=False, missing=True))
        report.write_text(json.dumps(recs))
        return subprocess.CompletedProcess(cmd, returncode, "", "")
    return run


class FakePhotosApp:
    """Runner standing in for `osascript … export … with using originals`:
    writes the given file names into the destination dir per UUID."""

    def __init__(self, exports: dict[str, list[str]], returncode: int = 0):
        self.exports = exports
        self.returncode = returncode
        self.calls: list[str] = []

    def __call__(self, cmd):
        assert cmd[0] == "osascript" and "with using originals" in cmd[2]
        uuid, dest = cmd[-2].split("/")[0], Path(cmd[-1])
        assert cmd[-2] == f"{uuid}/L0/001"
        self.calls.append(uuid)
        for name in self.exports.get(uuid, []):
            _touch(dest / name, name.encode())
        return subprocess.CompletedProcess(cmd, self.returncode, "", "" if not self.returncode else "-1728")


def test_export_batch_records_complete_and_requeues_incomplete(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["OK", "HALF"])
    run = _fake_osxphotos({
        "OK": ["2026/09/IMG_1.HEIC", "2026/09/IMG_1.mov", "2026/09/IMG_1.HEIC.xmp"],
        "HALF": ["2026/09/IMG_2.HEIC"],
    })
    batch, check = pp.export_batch(conn, _photos_db({"OK", "HALF"}), ["OK", "HALF"],
                                   tmp_path / "exp", run=run)
    assert batch is not None
    bdir = tmp_path / "exp" / batch
    assert not (bdir / ".osxphotos_export.db").exists()
    assert not (bdir / "2026/09/IMG_2.HEIC").exists()
    assert sorted(pp.local_listing(bdir)) == sorted([
        "2026/09/IMG_1.HEIC", "2026/09/IMG_1.mov", "2026/09/IMG_1.HEIC.xmp", REPORT_NAME,
    ])
    st = dict(conn.execute("SELECT uuid, status FROM uuid_state"))
    assert st == {"OK": pp.EXPORTED, "HALF": pp.FAILED}
    assert pp.undelivered_batches(conn) == [batch]


def test_export_batch_with_nothing_whole_leaves_no_batch(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["X"])
    batch, _ = pp.export_batch(conn, _photos_db(set()), ["X"], tmp_path / "exp",
                               run=_fake_osxphotos({}))
    assert batch is None
    assert list((tmp_path / "exp").iterdir()) == []
    assert conn.execute("SELECT status, attempts FROM uuid_state").fetchone() == (pp.FAILED, 1)


# --- Photos.app fallback --------------------------------------------------


def test_fallback_completes_a_live_photo_photokit_left_without_video(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["LIVE", "OK"])
    run = _fake_osxphotos(
        {"LIVE": ["2026/09/IMG_7.HEIC", "2026/09/IMG_7.HEIC.json"], "OK": ["2026/09/IMG_1.jpg"]},
        missing={"LIVE": ["2026/09/IMG_7.mov"]},
    )
    app = FakePhotosApp({"LIVE": ["IMG_7.HEIC", "IMG_7.MOV", "IMG_7.AAE"]})
    batch, check = pp.export_batch(conn, _photos_db({"LIVE"}), ["LIVE", "OK"],
                                   tmp_path / "exp", run=run, fallback_run=app)
    assert app.calls == ["LIVE"]
    assert check.rescued == ["LIVE"] and check.incomplete == {}
    bdir = tmp_path / "exp" / batch
    assert sorted(pp.local_listing(bdir)) == sorted([
        "2026/09/IMG_1.jpg", "2026/09/IMG_7.HEIC", "2026/09/IMG_7.HEIC.json",
        "2026/09/IMG_7.MOV", REPORT_NAME,
    ])
    assert (bdir / "2026/09/IMG_7.HEIC").read_bytes() == b"IMG_7.HEIC"   # Photos.app's copy
    assert [p.name for p in (tmp_path / "exp").iterdir()] == [batch]     # scratch removed
    recs = [r for r in json.loads((bdir / REPORT_NAME).read_text()) if r["uuid"] == "LIVE"]
    assert not any(r["missing"] or r["error"] for r in recs)
    assert sorted(Path(r["filename"]).name for r in recs if r.get("exported_by") == "photos-app") \
        == ["IMG_7.HEIC", "IMG_7.MOV"]
    assert dict(conn.execute("SELECT uuid, status FROM uuid_state")) == \
        {"LIVE": pp.EXPORTED, "OK": pp.EXPORTED}


def test_fallback_files_carry_the_uuid_for_dedup(tmp_path) -> None:
    from immy.dedup import photos as adapter
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["LIVE"])
    run = _fake_osxphotos({"LIVE": ["2026/09/IMG_7.HEIC"]}, missing={"LIVE": ["2026/09/IMG_7.mov"]})
    app = FakePhotosApp({"LIVE": ["IMG_7.HEIC", "IMG_7.MOV"]})
    batch, _ = pp.export_batch(conn, _photos_db({"LIVE"}), ["LIVE"], tmp_path / "exp",
                               run=run, fallback_run=app)
    mov = tmp_path / "exp" / batch / "2026/09/IMG_7.MOV"
    assert adapter.uuid_for(mov) == "LIVE"
    assert adapter.component_for(mov, "LIVE") == "live_video"


def test_fallback_that_still_lacks_the_video_is_pruned_and_retried(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["LIVE"])
    run = _fake_osxphotos({"LIVE": ["2026/09/IMG_7.HEIC"]}, missing={"LIVE": ["2026/09/IMG_7.mov"]})
    app = FakePhotosApp({"LIVE": ["IMG_7.HEIC"]})
    batch, check = pp.export_batch(conn, _photos_db({"LIVE"}), ["LIVE"], tmp_path / "exp",
                                   run=run, fallback_run=app)
    assert batch is None and check.rescued == []
    assert check.incomplete["LIVE"] == \
        "missing in Photos library; Photos.app: live photo without its video"
    assert list((tmp_path / "exp").iterdir()) == []
    assert conn.execute("SELECT status, attempts FROM uuid_state").fetchone() == (pp.FAILED, 1)


def test_fallback_error_is_recorded(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["X"])
    app = FakePhotosApp({}, returncode=1)
    _, check = pp.export_batch(conn, _photos_db(set()), ["X"], tmp_path / "exp",
                               run=_fake_osxphotos({}), fallback_run=app)
    assert check.incomplete["X"].startswith("nothing exported; Photos.app: osascript exited 1")


def test_fallback_places_unexported_asset_by_date_without_clobbering(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["A", "B"])
    run = _fake_osxphotos({"A": ["2026/09/IMG_5.HEIC"]})
    app = FakePhotosApp({"B": ["IMG_5.HEIC"]})
    sept_2026 = 1789000000 - pp._APPLE_EPOCH                      # 2026-09-09 UTC
    batch, check = pp.export_batch(conn, _photos_db(set(), {"B": sept_2026}), ["A", "B"],
                                   tmp_path / "exp", run=run, fallback_run=app)
    bdir = tmp_path / "exp" / batch
    assert check.rescued == ["B"]
    assert (bdir / "2026/09/IMG_5.HEIC").read_bytes() == b"x"          # A untouched
    assert (bdir / "2026/09/IMG_5 (1).HEIC").read_bytes() == b"IMG_5.HEIC"


def test_no_fallback_when_osxphotos_itself_failed(tmp_path) -> None:
    conn = pp.open_ledger(tmp_path / "l.sqlite")
    pp.enqueue(conn, ["X"])
    app = FakePhotosApp({"X": ["IMG_1.HEIC"]})
    batch, check = pp.export_batch(conn, _photos_db(set()), ["X"], tmp_path / "exp",
                                   run=_fake_osxphotos({}, returncode=1), fallback_run=app)
    assert batch is None and app.calls == []
    assert check.incomplete == {"X": "nothing exported"}


# --- delivery with a fake ssh/rsync acting on a local dir -----------------


class FakeRemote:
    """Interprets the exact ssh/rsync commands `deliver` issues against a
    local directory standing in for n5."""

    def __init__(self, root: Path, fail_rsync: bool = False, corrupt: bool = False):
        self.root = root
        self.fail_rsync = fail_rsync
        self.corrupt = corrupt
        self.rsyncs = 0

    def __call__(self, cmd):
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        bad = subprocess.CompletedProcess(cmd, 1, "", "boom")
        if cmd[0] == "ssh":
            cmd = ["ssh", cmd[-2], cmd[-1]]   # drop SSH_OPTS: host, script last
        if cmd[0] == "rsync":
            self.rsyncs += 1
            if self.fail_rsync:
                return bad
            src, dst = Path(cmd[-2]), Path(cmd[-1].split(":", 1)[1])
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            if self.corrupt:
                next(p for p in dst.rglob("*") if p.is_file()).write_bytes(b"")
            return ok
        script = cmd[2]
        if script.startswith("mkdir -p"):
            for part in script.split()[2:]:
                Path(part.strip("'")).mkdir(parents=True, exist_ok=True)
            return ok
        if script.startswith("test -d"):
            path = Path(script.split()[2].strip("'"))
            if not path.is_dir():
                return bad
            lines = [f"{p.stat().st_size} {p.relative_to(path).as_posix()}"
                     for p in path.rglob("*") if p.is_file()]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(lines) + "\n", "")
        if script.startswith("test ! -e"):
            parts = script.split()
            ready, staging = Path(parts[3].strip("'")), Path(parts[6].strip("'"))
            if ready.exists():
                return bad
            staging.rename(ready)
            return ok
        raise AssertionError(f"unexpected command {cmd}")


def _batch(tmp_path: Path) -> Path:
    b = tmp_path / "local" / "photos-20261003T000000Z-0001"
    _touch(b / "2026/09/IMG_1.HEIC", b"heic")
    _touch(b / REPORT_NAME, b"[]")
    return b


def test_deliver_publishes_to_ready(tmp_path) -> None:
    b = _batch(tmp_path)
    remote_root = tmp_path / "n5"
    pp.deliver(b, pp.Remote("n5", str(remote_root)), run=FakeRemote(remote_root))
    assert (remote_root / "ready" / b.name / "2026/09/IMG_1.HEIC").read_bytes() == b"heic"
    assert not (remote_root / ".staging" / b.name).exists()


def test_deliver_is_idempotent_after_publish(tmp_path) -> None:
    b = _batch(tmp_path)
    remote_root = tmp_path / "n5"
    fake = FakeRemote(remote_root)
    remote = pp.Remote("n5", str(remote_root))
    pp.deliver(b, remote, run=fake)
    pp.deliver(b, remote, run=fake)   # crash-before-ledger-update replay
    assert fake.rsyncs == 1


def test_deliver_refuses_on_rsync_failure_or_listing_mismatch(tmp_path) -> None:
    b = _batch(tmp_path)
    remote = pp.Remote("n5", str(tmp_path / "n5"))
    with pytest.raises(pp.DeliveryError, match="rsync failed"):
        pp.deliver(b, remote, run=FakeRemote(tmp_path / "n5", fail_rsync=True))
    with pytest.raises(pp.DeliveryError, match="listing differs"):
        pp.deliver(b, remote, run=FakeRemote(tmp_path / "n5", corrupt=True))
    assert not (tmp_path / "n5" / "ready" / b.name).exists()


def test_remote_parse() -> None:
    assert pp.Remote.parse("n5:/mnt/x/").root == "/mnt/x"
    with pytest.raises(ValueError):
        pp.Remote.parse("/mnt/x")
    with pytest.raises(ValueError):
        pp.Remote.parse("n5:rel/path")
