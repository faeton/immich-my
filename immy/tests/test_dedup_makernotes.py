"""Apple MakerNotes reach the manifest (audit 2026-10, item 2).

exiftool `-fast2` skips maker notes entirely, so every fingerprint ever
taken under it recorded `burst_uuid` and `live_cid` as NULL — on n5's live
manifest that was every one of ~290k rows, and the burst / Live-pair
guards in `_decide_one` never had anything to look at. Measured on real
iPhone HEICs in originals/2026/03: `-fast2` returns 0 of 164
MakerNotes:ContentIdentifier, `-fast` returns all 164.

The fixture is a 16x12 synthetic JPEG carrying the MakerNotes block copied
from one of those real HEICs (no pixels, no GPS): ContentIdentifier
FF191775-…, which `-fast2` cannot see. exiftool cannot write BurstUUID, so
burst behaviour is driven through `_exiftool_batch` instead.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from immy import exif
from immy.dedup import engine, manifest

FIXTURE = Path(__file__).parent / "fixtures" / "dedup-makernotes" / "apple-live-still.jpg"
FIXTURE_CID = "FF191775-F54A-4C0C-BB6E-BA3341E23C0B"

needs_exiftool = pytest.mark.skipif(
    shutil.which("exiftool") is None, reason="exiftool not installed"
)


def _staged(tmp_path: Path, name: str = "IMG_0012.JPG") -> Path:
    dst = tmp_path / "staging" / "icloud" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE, dst)
    return dst


# ---------------------------------------------------------------- reading


@needs_exiftool
def test_fingerprint_records_the_live_photo_content_identifier(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _staged(tmp_path)
    manifest.register(conn, "icloud", staged.parent)

    ok, failed = engine.fingerprint_pending(conn)

    assert (ok, failed) == (1, 0)
    live_cid, = conn.execute("SELECT live_cid FROM asset").fetchone()
    assert live_cid == FIXTURE_CID


@needs_exiftool
def test_read_folder_sees_maker_notes(tmp_path):
    staged = _staged(tmp_path)

    rows = exif.read_folder(staged.parent)

    assert [r.get("MakerNotes:ContentIdentifier") for r in rows] == [FIXTURE_CID]


# ---------------------------------------------------- refreshing old rows


def _insert(conn, asset_id, path, *, status, source="icloud", dest_path=None,
            sha256="ab" * 32, burst=None, live=None, edited=0):
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, media_type, format,"
        " width, height, taken_at, taken_src, phash, exif_fields, sha256, dest_path,"
        " burst_uuid, live_cid, edited)"
        " VALUES (?, ?, ?, ?, 2200, 'image', 'jpg', 16, 12, '2026-03-01T10:00:00',"
        " 'exif', 'aaaa5555aaaa5555', 40, ?, ?, ?, ?, ?)",
        (asset_id, source, str(path), status, sha256, dest_path, burst, live, edited),
    )


def _cluster(conn, cluster_id, decision, winner, members):
    conn.execute("INSERT INTO cluster (id, decision, winner_asset_id) VALUES (?, ?, ?)",
                 (cluster_id, decision, winner))
    for m in members:
        conn.execute("INSERT INTO membership (cluster_id, asset_id, role) VALUES (?, ?, ?)",
                     (cluster_id, m, "winner" if m == winner else "loser"))


def _row(conn, asset_id):
    conn.row_factory = None
    return conn.execute(
        "SELECT status, bytes, sha256, dest_path, burst_uuid, live_cid, edited"
        " FROM asset WHERE id=?", (asset_id,)
    ).fetchone()


@needs_exiftool
def test_refresh_fills_live_cid_from_where_the_file_now_lives(tmp_path):
    """A promoted row's staging path is gone; the bytes are at dest_path."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = tmp_path / "originals" / "2026" / "03" / "IMG_0012.JPG"
    lib.parent.mkdir(parents=True)
    shutil.copy2(FIXTURE, lib)
    _insert(conn, 1, tmp_path / "staging" / "IMG_0012.JPG",
            status=manifest.PROMOTED, dest_path=str(lib))
    conn.commit()
    before = _row(conn, 1)

    result = engine.refresh_metadata(conn)

    after = _row(conn, 1)
    assert after[5] == FIXTURE_CID
    assert after[:4] == before[:4]              # status/bytes/sha256/dest untouched
    assert result["updated"] == 1 and result["missing"] == 0
    # Idempotent: nothing left to gain on a second pass.
    again = engine.refresh_metadata(conn)
    assert again["updated"] == 0 and _row(conn, 1) == after


def test_refresh_counts_a_missing_file_and_changes_nothing(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _insert(conn, 1, tmp_path / "gone.JPG", status=manifest.CLUSTERED)
    conn.commit()
    before = _row(conn, 1)

    result = engine.refresh_metadata(conn)

    assert result["missing"] == 1 and result["updated"] == 0
    assert _row(conn, 1) == before


def test_refresh_never_overwrites_a_known_value(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    f = tmp_path / "a.JPG"
    f.write_bytes(b"x")
    _insert(conn, 1, f, status=manifest.FINGERPRINTED, live="KEEP-ME", edited=1)
    conn.commit()
    monkeypatch.setattr(engine, "_exiftool_batch", lambda paths: {
        p: {"SourceFile": p, "MakerNotes:ContentIdentifier": "OTHER"} for p in paths
    })

    engine.refresh_metadata(conn)

    assert _row(conn, 1)[5:] == ("KEEP-ME", 1)


def _fake_burst(monkeypatch, uuid="BURST-1"):
    monkeypatch.setattr(engine, "_exiftool_batch", lambda paths: {
        p: {"SourceFile": p, "MakerNotes:BurstUUID": uuid} for p in paths
    })


def _files(tmp_path, *names):
    out = []
    for n in names:
        f = tmp_path / "staging" / n
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(n.encode())
        out.append(f)
    return out


def test_refresh_reopens_an_unapplied_auto_cluster_that_gained_a_burst(tmp_path, monkeypatch):
    """Two burst frames auto-merged because the guard never saw BurstUUID.
    Once the id is known the cluster must be decided again — and the burst
    guard keeps both."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    a, b = _files(tmp_path, "IMG_1.JPG", "IMG_2.JPG")
    _insert(conn, 1, a, status=manifest.DECIDED)
    _insert(conn, 2, b, status=manifest.DECIDED)
    _cluster(conn, 1, "auto", 1, (1, 2))
    conn.commit()
    _fake_burst(monkeypatch)

    result = engine.refresh_metadata(conn)

    assert result["clusters_reopened"] == 1
    assert conn.execute("SELECT decision FROM cluster WHERE id=1").fetchone()[0] == "pending"
    assert {r[0] for r in conn.execute("SELECT status FROM asset")} == {manifest.CLUSTERED}
    assert engine.decide(conn)["kept_all"] == 1


def test_refresh_never_unapplies_an_applied_cluster(tmp_path, monkeypatch):
    """Winner promoted, loser quarantined: the moves stand. The cluster is
    reported, not reopened."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    w, l = _files(tmp_path, "W.JPG", "L.JPG")
    _insert(conn, 1, tmp_path / "staging-gone-W.JPG", status=manifest.PROMOTED,
            dest_path=str(w))
    _insert(conn, 2, tmp_path / "staging-gone-L.JPG", status=manifest.QUARANTINED,
            dest_path=str(l))
    _cluster(conn, 1, "auto", 1, (1, 2))
    conn.commit()
    _fake_burst(monkeypatch)

    result = engine.refresh_metadata(conn)

    assert result["updated"] == 2
    assert (result["clusters_reopened"], result["applied_clusters_affected"]) == (0, 1)
    assert conn.execute("SELECT decision FROM cluster WHERE id=1").fetchone()[0] == "auto"
    assert [r[0] for r in conn.execute("SELECT status FROM asset ORDER BY id")] == [
        manifest.PROMOTED, manifest.QUARANTINED]


def test_refresh_holds_the_unapplied_rest_of_a_partly_applied_cluster(tmp_path, monkeypatch):
    """Winner promoted, loser still `decided` (its move failed last run). The
    loser must not be quarantined on the burst-blind decision: it drops back
    to `clustered`, which promote-rest treats as a keeper."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    w, l = _files(tmp_path, "W.JPG", "L.JPG")
    _insert(conn, 1, tmp_path / "gone-W.JPG", status=manifest.PROMOTED, dest_path=str(w))
    _insert(conn, 2, l, status=manifest.DECIDED)
    _cluster(conn, 1, "auto", 1, (1, 2))
    conn.commit()
    _fake_burst(monkeypatch)

    result = engine.refresh_metadata(conn)

    assert result["applied_clusters_affected"] == 1
    assert [r[0] for r in conn.execute("SELECT status FROM asset ORDER BY id")] == [
        manifest.PROMOTED, manifest.CLUSTERED]


def test_refresh_leaves_clusters_alone_when_nothing_was_gained(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    a, b = _files(tmp_path, "IMG_1.JPG", "IMG_2.JPG")
    _insert(conn, 1, a, status=manifest.DECIDED)
    _insert(conn, 2, b, status=manifest.DECIDED)
    _cluster(conn, 1, "auto", 1, (1, 2))
    conn.commit()
    monkeypatch.setattr(engine, "_exiftool_batch",
                        lambda paths: {p: {"SourceFile": p} for p in paths})

    result = engine.refresh_metadata(conn)

    assert (result["updated"], result["clusters_reopened"]) == (0, 0)
    assert conn.execute("SELECT decision FROM cluster WHERE id=1").fetchone()[0] == "auto"
    assert {r[0] for r in conn.execute("SELECT status FROM asset")} == {manifest.DECIDED}


@needs_exiftool
def test_cli_fingerprint_refresh_meta(tmp_path):
    from typer.testing import CliRunner

    from immy.cli import app

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _staged(tmp_path)
    _insert(conn, 1, staged, status=manifest.FINGERPRINTED)
    conn.commit()
    conn.close()

    res = CliRunner().invoke(app, [
        "dedup", "fingerprint", "--manifest", str(tmp_path / "m.sqlite"), "--refresh-meta",
    ])

    assert res.exit_code == 0, res.output
    assert "1 updated" in res.output
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    assert conn.execute("SELECT live_cid FROM asset").fetchone()[0] == FIXTURE_CID


def test_cli_refresh_meta_waits_for_a_mover(tmp_path):
    """It demotes `decided` rows, so it must not run under an apply."""
    from typer.testing import CliRunner

    from immy.cli import MOVERS_LOCK_SUFFIX, app

    m = tmp_path / "m.sqlite"
    manifest.open_manifest(m).close()
    m.with_suffix(MOVERS_LOCK_SUFFIX).touch()

    res = CliRunner().invoke(app, ["dedup", "fingerprint", "--manifest", str(m), "--refresh-meta"])

    assert res.exit_code == 1 and "in progress" in " ".join(res.output.split())
