"""Phase 2 identity (todo/PHASE2-IDENTITY-DESIGN.md) — schema v4.

The migration must be restart-safe against n5's live manifest: an interrupt
leaves either v3 with none of the new columns or v4 with all of them, and a
retry finishes the job.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from immy.dedup import manifest

V3_ASSET = """
CREATE TABLE asset (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL, bytes INTEGER, mtime REAL, media_type TEXT, format TEXT,
  width INTEGER, height INTEGER, taken_at TEXT, taken_src TEXT, gps_lat REAL,
  gps_lon REAL, phash TEXT, exif_fields INTEGER, burst_uuid TEXT, live_cid TEXT,
  edited INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE TABLE cluster (id INTEGER PRIMARY KEY, winner_asset_id INTEGER,
  confidence REAL, decision TEXT NOT NULL DEFAULT 'pending', clip_cos_sim REAL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '3');
INSERT INTO asset (id, source, path, status, bytes)
  VALUES (1, 'icloud', '/staging/icloud/a.jpg', 'promoted', 10);
"""


def _v3(path: Path) -> Path:
    raw = sqlite3.connect(path)
    raw.executescript(V3_ASSET)
    raw.close()
    return path


def _asset_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(asset)")}


def _indexes(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_v3_manifest_migrates_to_v4_keeping_rows(tmp_path):
    conn = manifest.open_manifest(_v3(tmp_path / "m.sqlite"))

    assert manifest.get_meta(conn, "schema_version") == "4"
    assert {name for name, _ in manifest.V4_ASSET_COLUMNS} <= _asset_columns(conn)
    assert {"idx_asset_identity", "idx_asset_sha256", "idx_library_file_sha256"} <= _indexes(conn)
    assert conn.execute("SELECT path, status FROM asset").fetchall() == [
        ("/staging/icloud/a.jpg", "promoted"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 0


def test_fresh_manifest_is_v4(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    assert manifest.get_meta(conn, "schema_version") == "4"
    assert {name for name, _ in manifest.V4_ASSET_COLUMNS} <= _asset_columns(conn)


def test_interrupted_migration_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    path = _v3(tmp_path / "m.sqlite")
    real = manifest._add_missing_columns
    calls = []

    def dies_after_first_asset_column(conn, table, columns):
        if table == "asset":
            real(conn, table, list(columns)[:1])       # one ALTER lands…
            calls.append(table)
            raise KeyboardInterrupt                    # …then the process is killed
        real(conn, table, columns)

    monkeypatch.setattr(manifest, "_add_missing_columns", dies_after_first_asset_column)
    with pytest.raises(KeyboardInterrupt):
        manifest.open_manifest(path)
    assert calls == ["asset"]

    raw = sqlite3.connect(path)
    assert raw.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("3",)
    assert "source_uid" not in _asset_columns(raw)     # the ALTER rolled back with it
    raw.close()

    monkeypatch.setattr(manifest, "_add_missing_columns", real)
    conn = manifest.open_manifest(path)
    assert manifest.get_meta(conn, "schema_version") == "4"
    assert {name for name, _ in manifest.V4_ASSET_COLUMNS} <= _asset_columns(conn)


def test_partially_migrated_schema_without_version_bump_completes(tmp_path):
    """Belt and braces: columns already present under an old version number
    (the pre-v4 failure mode the review flagged) must not die on
    'duplicate column name'."""
    path = _v3(tmp_path / "m.sqlite")
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE asset ADD COLUMN source_uid TEXT")
    raw.execute("ALTER TABLE asset ADD COLUMN component TEXT")
    raw.commit()
    raw.close()

    conn = manifest.open_manifest(path)
    assert manifest.get_meta(conn, "schema_version") == "4"
    assert {name for name, _ in manifest.V4_ASSET_COLUMNS} <= _asset_columns(conn)


def test_missing_version_row_is_inspected_not_trusted(tmp_path):
    path = _v3(tmp_path / "m.sqlite")
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM meta")
    raw.commit()
    raw.close()

    conn = manifest.open_manifest(path)
    assert manifest.get_meta(conn, "schema_version") == "4"
    assert "sha256" in _asset_columns(conn)


def test_v4_reopen_is_idempotent(tmp_path):
    path = tmp_path / "m.sqlite"
    manifest.open_manifest(path).close()
    conn = manifest.open_manifest(path)
    assert manifest.get_meta(conn, "schema_version") == "4"


# =================================================================== identity
#
# The principle under test: a staging file is disposed of as a duplicate only
# when the library, right now, holds the exact same bytes.

import hashlib
import os

from immy.dedup import engine, identity


def _jpeg(path: Path, seed: int) -> Path:
    import numpy as np
    import pyvips

    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 255, size=(64, 64), dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    pyvips.Image.new_from_memory(pixels.tobytes(), 64, 64, 1, "uchar").jpegsave(str(path), Q=90)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _register(conn, source: str, path: Path, status: str = manifest.REGISTERED) -> int:
    cur = conn.execute(
        "INSERT INTO asset (source, path, status, bytes, format) VALUES (?, ?, ?, ?, ?)",
        (source, str(path), status, path.stat().st_size, path.suffix.lstrip(".").lower()),
    )
    conn.commit()
    return cur.lastrowid


def _row(conn, asset_id: int) -> dict:
    cur = conn.execute("SELECT * FROM asset WHERE id=?", (asset_id,))
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


# ----------------------------------------------------------- hash_stable


def test_hash_stable_refuses_symlinks(tmp_path):
    real = tmp_path / "a.jpg"
    real.write_bytes(b"x")
    (tmp_path / "link.jpg").symlink_to(real)
    with pytest.raises(identity.UnstableFile):
        identity.hash_stable(tmp_path / "link.jpg")


def test_hash_stable_detects_a_change_during_the_read(tmp_path, monkeypatch):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"old")
    real_lstat, calls = os.lstat, []

    def lstat(p, *a, **kw):
        calls.append(p)
        if len(calls) == 2:                      # the "after" stat
            f.write_bytes(b"newer bytes")
        return real_lstat(p, *a, **kw)

    monkeypatch.setattr(identity.os, "lstat", lstat)
    with pytest.raises(identity.UnstableFile):
        identity.hash_stable(f)


# ---------------------------------------------------------- index_library


def test_index_library_hashes_skips_and_prunes(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = tmp_path / "originals"
    keep = lib / "2026" / "07" / "IMG_1.JPG"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"one")
    (lib / "2026" / "07" / ".IMG_2.JPG.123.partial").write_bytes(b"tmp")   # dotfile
    (lib / "2026" / "07" / ".immy-new.video.mp4").write_bytes(b"triage tmp")  # media ext
    (lib / ".trash").mkdir()
    (lib / ".trash" / "IMG_3.JPG").write_bytes(b"hidden dir")
    (lib / "2026" / "07" / "notes.txt").write_bytes(b"not media")
    (lib / "2026" / "07" / "IMG_4.JPG").symlink_to(keep)                  # symlink

    first = identity.index_library(conn, [lib])
    assert (first.hashed, first.skipped) == (1, 1)
    assert conn.execute("SELECT path, sha256 FROM library_file").fetchall() == [
        (str(keep), _sha(keep)),
    ]

    again = identity.index_library(conn, [lib])
    assert (again.hashed, again.unchanged) == (0, 1)                      # resumable

    keep.write_bytes(b"one, edited")
    changed = identity.index_library(conn, [lib])
    assert changed.hashed == 1
    assert conn.execute("SELECT sha256 FROM library_file").fetchone()[0] == _sha(keep)

    keep.unlink()
    assert identity.index_library(conn, [lib]).pruned == 1
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 0


def test_index_library_subtree_only_prunes_inside_it(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = tmp_path / "originals"
    old = lib / "2019" / "a.jpg"
    new = lib / "2026" / "b.jpg"
    for f in (old, new):
        f.parent.mkdir(parents=True)
        f.write_bytes(f.name.encode())
    identity.index_library(conn, [lib])
    old.unlink()
    result = identity.index_library(conn, [lib / "2026"])
    assert result.pruned == 0                                              # 2019 not walked
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 2


# ---------------------------------------------------------- library_match


def test_library_match_rehashes_a_stale_row(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = tmp_path / "originals" / "a.jpg"
    lib.parent.mkdir()
    lib.write_bytes(b"original")
    identity.index_library(conn, [lib.parent])
    old_sha = _sha(lib)

    lib.write_bytes(b"replaced by triage")
    assert identity.library_match(conn, old_sha) is None                   # no longer holds it
    assert conn.execute("SELECT sha256 FROM library_file").fetchone()[0] == _sha(lib)


def test_library_match_never_matches_the_staging_file_itself(tmp_path):
    """A hardlink (or bind-mount view) of the staging file is the same file:
    quarantining one of its names is not deduplication."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staging = tmp_path / "staging" / "a.jpg"
    staging.parent.mkdir()
    staging.write_bytes(b"bytes")
    lib = tmp_path / "originals" / "a.jpg"
    lib.parent.mkdir()
    os.link(staging, lib)
    identity.index_library(conn, [lib.parent])
    assert identity.library_match(conn, _sha(staging), not_same_as=staging) is None


# -------------------------------------------------------------- fingerprint


def _library_with(tmp_path, conn, content_from: Path) -> Path:
    lib = tmp_path / "originals" / "2026" / "07" / content_from.name
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_bytes(content_from.read_bytes())
    identity.index_library(conn, [tmp_path / "originals"])
    return lib


def test_fingerprint_aliases_bytes_the_library_already_holds(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _jpeg(tmp_path / "staging" / "photos" / "IMG_1.JPG", seed=1)
    lib = _library_with(tmp_path, conn, staged)
    other = _jpeg(tmp_path / "staging" / "photos" / "IMG_2.JPG", seed=2)
    a, b = _register(conn, "photos", staged), _register(conn, "photos", other)

    stats: dict = {}
    ok, failed = engine.fingerprint_pending(conn, stats=stats)

    assert (ok, failed, stats.get("alias")) == (2, 0, 1)
    ra, rb = _row(conn, a), _row(conn, b)
    assert (ra["status"], ra["alias_path"], ra["sha256"]) == (manifest.ALIAS, str(lib), _sha(staged))
    assert ra["phash"] is None                                             # never clustered
    assert (rb["status"], rb["alias_path"]) == (manifest.FINGERPRINTED, None)
    assert rb["sha256"] == _sha(other) and rb["phash"]


def test_fingerprint_never_aliases_originals_and_indexes_them(tmp_path):
    """Bootstrap registers library files as `originals`: they are the library,
    so they must never alias (and then be quarantined) — they build the index."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    first = _jpeg(tmp_path / "originals" / "trip" / "a.jpg", seed=3)
    twin = tmp_path / "originals" / "trip-copy" / "a.jpg"
    twin.parent.mkdir(parents=True)
    twin.write_bytes(first.read_bytes())
    ids = [_register(conn, "originals", first), _register(conn, "originals", twin)]

    engine.fingerprint_pending(conn)

    assert {_row(conn, i)["status"] for i in ids} == {manifest.FINGERPRINTED}
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 2


def test_fingerprint_zero_byte_file_is_a_stub(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    stub = tmp_path / "staging" / "IMG_9.HEIC"
    stub.parent.mkdir(parents=True)
    stub.write_bytes(b"")
    asset_id = _register(conn, "photos", stub)
    engine.fingerprint_pending(conn)
    row = _row(conn, asset_id)
    assert row["status"] == manifest.ERROR and row["error"].startswith("stub:")


def test_fingerprint_sparse_icloudpd_placeholder_is_a_stub(tmp_path):
    """The shape of n5's staging/icloud tree: full apparent size, no data."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    stub = tmp_path / "staging" / "icloud" / "IMG_1724.HEIC"
    stub.parent.mkdir(parents=True)
    with open(stub, "wb") as f:
        f.truncate(894_295)
    asset_id = _register(conn, "icloud", stub)
    engine.fingerprint_pending(conn)
    assert _row(conn, asset_id)["error"] == "stub: zero-filled placeholder (sparse stub)"


def test_fingerprint_media_named_text_file_is_a_stub(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    page = tmp_path / "staging" / "IMG_9.MOV"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"<html>503 Service Unavailable</html>")
    asset_id = _register(conn, "photos", page)
    engine.fingerprint_pending(conn)
    assert _row(conn, asset_id)["error"].startswith("stub: content is text/")


def test_fingerprint_uid_revision_is_held_and_uid_needs_a_component(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    promoted = _jpeg(tmp_path / "staging" / "old" / "IMG_1.JPG", seed=4)
    conn.execute(
        "INSERT INTO asset (source, path, status, source_uid, component, sha256)"
        " VALUES ('photos', '/gone/IMG_1.JPG', 'promoted', 'UUID-1', 'original', ?)",
        ("f" * 64,),
    )
    edited = _jpeg(tmp_path / "staging" / "new" / "IMG_1.JPG", seed=5)
    no_component = _jpeg(tmp_path / "staging" / "new" / "IMG_7.JPG", seed=6)
    rev_id = _register(conn, "photos", edited)
    bad_id = _register(conn, "photos", no_component)
    uids = {str(edited): ("UUID-1", "original"), str(no_component): ("UUID-7", None)}

    real = engine.fingerprint_fields

    def with_identity(row, source):
        fields = real(row, source)
        uid, comp = uids[str(row.path)]
        fields["source_uid"], fields["component"] = uid, comp
        return fields

    monkeypatch.setattr(engine, "fingerprint_fields", with_identity)
    engine.fingerprint_pending(conn)

    assert _row(conn, rev_id)["error"].startswith("revision of #")
    assert _row(conn, bad_id)["error"] == "source_uid without component"
    assert promoted.exists()


def test_write_fingerprint_is_conditional_on_registered(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    asset_id = _register(conn, "icloud", f, status=manifest.FINGERPRINTED)
    assert manifest.write_fingerprint(conn, asset_id, {"width": 1}) is False
    with pytest.raises(ValueError):
        manifest.write_fingerprint(conn, asset_id, {"source_uid": "U"})


# -------------------------------------------------------------------- moves


def _staged_promotable(tmp_path, conn, data: bytes, name="IMG_5.JPG") -> tuple[int, Path]:
    src = tmp_path / "staging" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    cur = conn.execute(
        "INSERT INTO asset (source, path, status, bytes, taken_at, media_type)"
        " VALUES ('icloud', ?, ?, ?, '2026-07-12T10:00:00', 'image')",
        (str(src), manifest.FINGERPRINTED, len(data)),
    )
    conn.commit()
    return cur.lastrowid, src


def test_promote_records_dest_and_hash_and_indexes_the_library(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"promote me")
    originals = tmp_path / "originals"
    engine.promote_rest(conn, originals_root=originals, dry_run=False)
    dest = originals / "2026" / "07" / "IMG_5.JPG"
    row = _row(conn, asset_id)
    assert (row["status"], row["dest_path"], row["sha256"]) == (
        manifest.PROMOTED, str(dest), _sha(dest),
    )
    assert conn.execute("SELECT path FROM library_file").fetchall() == [(str(dest),)]


def test_crash_between_record_and_unlink_resumes_exactly(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"crash me")
    originals = tmp_path / "originals"
    real_unlink = Path.unlink

    def killed(self, *a, **kw):
        if self == src:
            raise KeyboardInterrupt
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", killed)
    with pytest.raises(KeyboardInterrupt):
        engine.promote_rest(conn, originals_root=originals, dry_run=False)
    conn.close()
    conn = manifest.open_manifest(tmp_path / "m.sqlite")      # what survived is durable
    row = _row(conn, asset_id)
    assert row["dest_path"] and row["status"] == manifest.FINGERPRINTED and src.exists()

    # Resume: the recorded path + hash identify the finished copy — no second
    # copy at a collision name, and the source is consumed only now.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    engine.promote_rest(conn, originals_root=originals, dry_run=False)
    assert _row(conn, asset_id)["status"] == manifest.PROMOTED
    assert not src.exists()
    assert list((originals / "2026" / "07").iterdir()) == [originals / "2026" / "07" / "IMG_5.JPG"]


def test_source_changed_after_record_is_never_consumed(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"version one")
    originals = tmp_path / "originals"
    real_unlink = Path.unlink

    def killed(self, *a, **kw):
        if self == src:
            raise KeyboardInterrupt
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", killed)
    with pytest.raises(KeyboardInterrupt):
        engine.promote_rest(conn, originals_root=originals, dry_run=False)
    monkeypatch.setattr(Path, "unlink", real_unlink)

    src.write_bytes(b"version TWO, never copied")
    result = engine.promote_rest(conn, originals_root=originals, dry_run=False)
    assert result["errors"] == 1 and "source changed" in result["error_samples"][0]
    assert src.read_bytes() == b"version TWO, never copied"


def test_sampled_windows_are_not_proof_before_an_unlink(tmp_path, monkeypatch):
    """Two >16 MB files that differ only outside content_equal's three
    sampled windows. Clustering may treat them as equal; `_resolve_dest`
    must not, because its yes unlinks the source."""
    monkeypatch.setattr(engine, "CONTENT_FULL_COMPARE_MAX", 1024)
    monkeypatch.setattr(engine, "CONTENT_SAMPLE_WINDOW", 256)
    size = 8192
    a = bytearray(b"\0" * size)
    b = bytearray(a)
    b[1500] = 1                                   # outside head/middle/tail windows
    src = tmp_path / "staging" / "v.mov"
    dest = tmp_path / "originals" / "v.mov"
    for path, data in ((src, a), (dest, b)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(data))
    assert engine.content_equal(src, dest)                           # the sampled view
    resolved, done = engine._resolve_dest(dest, 9, size, src)
    assert (resolved.name, done) == ("v__9.mov", False)             # full hash says no


# ----------------------------------------------------------- alias disposal


def test_apply_quarantines_a_proven_alias_and_keeps_the_reason(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _jpeg(tmp_path / "staging" / "photos" / "IMG_1.JPG", seed=7)
    lib = _library_with(tmp_path, conn, staged)
    asset_id = _register(conn, "photos", staged)
    engine.fingerprint_pending(conn)
    quarantine = tmp_path / "quarantine"

    dry = engine.apply_decisions(conn, originals_root=tmp_path / "originals",
                                 quarantine_root=quarantine, dry_run=True)
    assert dry["aliases_quarantined"] == 1 and staged.exists()

    result = engine.apply_decisions(conn, originals_root=tmp_path / "originals",
                                    quarantine_root=quarantine, dry_run=False)
    assert result["aliases_quarantined"] == 1 and result["errors"] == 0
    row = _row(conn, asset_id)
    assert (row["status"], row["alias_path"]) == (manifest.QUARANTINED, str(lib))
    assert not staged.exists() and Path(row["dest_path"]).read_bytes() == lib.read_bytes()
    assert lib.exists()


def test_apply_requeues_an_alias_whose_library_copy_changed(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _jpeg(tmp_path / "staging" / "photos" / "IMG_1.JPG", seed=8)
    lib = _library_with(tmp_path, conn, staged)
    asset_id = _register(conn, "photos", staged)
    engine.fingerprint_pending(conn)
    # Changed in place with size, inode AND mtime preserved — invisible to
    # the stat key, so only a full re-hash at disposal can catch it.
    st = os.stat(lib)
    data = bytearray(lib.read_bytes())
    data[-3] ^= 0xFF
    with open(lib, "r+b") as f:
        f.write(bytes(data))
    os.utime(lib, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _file_key(lib) == (st.st_size, st.st_ino, st.st_mtime_ns)

    result = engine.apply_decisions(conn, originals_root=tmp_path / "originals",
                                    quarantine_root=tmp_path / "quarantine", dry_run=False)
    assert result["aliases_requeued"] == 1
    row = _row(conn, asset_id)
    assert (row["status"], row["alias_path"]) == (manifest.REGISTERED, None)
    assert staged.exists()

    # No alias → requeue loop: the index row was corrected, so the next pass
    # fingerprints the arrival normally.
    engine.fingerprint_pending(conn)
    assert _row(conn, asset_id)["status"] == manifest.FINGERPRINTED


def _file_key(p: Path) -> tuple[int, int, int]:
    st = os.stat(p)
    return (st.st_size, st.st_ino, st.st_mtime_ns)


def test_apply_limit_is_shared_with_aliases(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _jpeg(tmp_path / "staging" / "photos" / "IMG_10.JPG", seed=10)
    _library_with(tmp_path, conn, staged)
    _register(conn, "photos", staged)
    engine.fingerprint_pending(conn)
    # One auto-decided cluster member queued ahead of the alias.
    winner, _ = _staged_promotable(tmp_path, conn, b"cluster winner", name="W.JPG")
    conn.execute("UPDATE asset SET status=? WHERE id=?", (manifest.DECIDED, winner))
    cid = conn.execute("INSERT INTO cluster (decision, winner_asset_id) VALUES ('auto', ?)",
                       (winner,)).lastrowid
    conn.execute("INSERT INTO membership (cluster_id, asset_id, role) VALUES (?, ?, 'winner')",
                 (cid, winner))
    conn.commit()

    kw = dict(originals_root=tmp_path / "originals", quarantine_root=tmp_path / "quarantine",
              dry_run=True)
    one = engine.apply_decisions(conn, limit=1, **kw)
    assert (one["promoted"], one["aliases_quarantined"]) == (1, 0)
    two = engine.apply_decisions(conn, limit=2, **kw)
    assert (two["promoted"], two["aliases_quarantined"]) == (1, 1)


# ------------------------------------------------------------- retry-errors


def test_retry_errors_resets_matching_rows_and_refreshes_size(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    stub = tmp_path / "IMG_1.HEIC"
    stub.write_bytes(b"")
    gone = tmp_path / "IMG_2.HEIC"
    a = _register(conn, "photos", stub)
    conn.execute("INSERT INTO asset (source, path, status, error) VALUES "
                 "('photos', ?, 'error', 'stub: 0 bytes')", (str(gone),))
    conn.execute("UPDATE asset SET status='error', error='stub: 0 bytes' WHERE id=?", (a,))
    conn.commit()
    stub.write_bytes(b"the real file now")

    assert manifest.retry_errors(conn, match="stub") == 1
    row = _row(conn, a)
    assert (row["status"], row["error"], row["bytes"]) == (manifest.REGISTERED, None, 17)


# ------------------------------------------------- review round 3 (code)


def _crash_after_record(tmp_path, conn, data=b"payload"):
    """Run a promotion that records its dest and dies before the unlink."""
    asset_id, src = _staged_promotable(tmp_path, conn, data)
    real_unlink = Path.unlink

    def killed(self, *a, **kw):
        if self == src:
            raise KeyboardInterrupt
        return real_unlink(self, *a, **kw)

    Path.unlink = killed
    try:
        with pytest.raises(KeyboardInterrupt):
            engine.promote_rest(conn, originals_root=tmp_path / "originals", dry_run=False)
    finally:
        Path.unlink = real_unlink
    return asset_id, src, Path(_row(conn, asset_id)["dest_path"])


def test_recorded_dest_replaced_by_a_symlink_to_the_source_is_not_done(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src, dest = _crash_after_record(tmp_path, conn)
    dest.unlink()
    dest.symlink_to(src)                         # hashes equal, holds nothing

    result = engine.promote_rest(conn, originals_root=tmp_path / "originals", dry_run=False)
    # The symlink is a collision, not this asset's copy: the bytes are
    # really copied to the collision name before the source is consumed.
    assert result["errors"] == 0
    landed = dest.with_name(f"{dest.stem}__{asset_id}{dest.suffix}")
    assert landed.read_bytes() == b"payload" and not landed.is_symlink()
    assert _row(conn, asset_id)["dest_path"] == str(landed)


def test_plain_dest_symlinked_to_the_source_is_a_collision_not_done(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"payload")
    plain = tmp_path / "originals" / "2026" / "07" / "IMG_5.JPG"
    plain.parent.mkdir(parents=True)
    plain.symlink_to(src)

    engine.promote_rest(conn, originals_root=tmp_path / "originals", dry_run=False)
    landed = plain.with_name(f"IMG_5__{asset_id}.JPG")
    assert landed.read_bytes() == b"payload" and not landed.is_symlink()
    assert not src.exists()


def test_post_unlink_pre_status_crash_finishes_from_the_record(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"payload")
    engine._move_asset(conn, asset_id, src, tmp_path / "originals" / "2026" / "07" / "IMG_5.JPG",
                       7, (None, None), library_root=tmp_path / "originals",
                       dest_root=tmp_path / "originals")
    conn.close()                                 # died before the status UPDATE
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    assert not src.exists() and _row(conn, asset_id)["status"] == manifest.FINGERPRINTED

    result = engine.promote_rest(conn, originals_root=tmp_path / "originals", dry_run=False)
    assert result["errors"] == 0 and _row(conn, asset_id)["status"] == manifest.PROMOTED
    assert len(list((tmp_path / "originals" / "2026" / "07").iterdir())) == 1


def test_source_replaced_during_the_move_is_left_in_place(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"version one")
    real_copy = engine._safe_copy

    def copy_then_replace(s, d):
        sha = real_copy(s, d)
        replacement = s.with_name("replacement")
        replacement.write_bytes(b"version two!")
        os.replace(replacement, s)               # new inode at the same path
        return sha

    monkeypatch.setattr(engine, "_safe_copy", copy_then_replace)
    result = engine.promote_rest(conn, originals_root=tmp_path / "originals", dry_run=False)
    assert result["errors"] == 1 and "source changed" in result["error_samples"][0]
    assert src.read_bytes() == b"version two!"


def test_fingerprint_error_cannot_overwrite_a_row_another_worker_advanced(tmp_path):
    path = tmp_path / "m.sqlite"
    conn = manifest.open_manifest(path)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    asset_id = _register(conn, "icloud", f)
    other = manifest.open_manifest(path)
    other.execute("UPDATE asset SET status='promoted' WHERE id=?", (asset_id,))
    other.commit()
    assert manifest.write_error(conn, asset_id, "stale", only_if=manifest.REGISTERED) is False
    conn.commit()
    assert _row(conn, asset_id)["status"] == manifest.PROMOTED


def test_library_eligible_rejects_hidden_components_anywhere():
    assert identity.library_eligible(Path("/originals/2026/07/IMG_1.JPG"))
    assert not identity.library_eligible(Path("/originals/.trash/IMG_1.JPG"))
    assert not identity.library_eligible(Path("/originals/2026/.immy-new.video.mp4"))


def test_index_library_dir_must_stay_inside_originals(tmp_path):
    from typer.testing import CliRunner

    from immy.cli import app

    lib = tmp_path / "originals"
    (lib / "2026").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    runner = CliRunner()
    for bad in ("../elsewhere", str(tmp_path / "elsewhere"), ".hidden"):
        (lib / ".hidden").mkdir(exist_ok=True)
        result = runner.invoke(app, ["dedup", "index-library", "--originals", str(lib),
                                     "--dir", bad, "--manifest", str(tmp_path / "m.sqlite")])
        assert result.exit_code == 1, (bad, result.output)
    ok = runner.invoke(app, ["dedup", "index-library", "--originals", str(lib),
                             "--dir", "2026", "--manifest", str(tmp_path / "m.sqlite")])
    assert ok.exit_code == 0, ok.output


def test_failed_dir_fsync_keeps_the_source_and_the_retry_resyncs(tmp_path, monkeypatch):
    """Rename landed, directory fsync failed: the source must survive, and the
    retry (which finds the copy `already_done`) must sync before consuming."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    asset_id, src = _staged_promotable(tmp_path, conn, b"payload")
    originals = tmp_path / "originals"
    real = engine._fsync_dir
    synced: list[Path] = []

    def failing(path):
        raise OSError(5, "EIO")

    monkeypatch.setattr(engine, "_fsync_dir", failing)
    monkeypatch.setattr(engine, "_mkdirs_durable", lambda p: p.mkdir(parents=True, exist_ok=True))
    result = engine.promote_rest(conn, originals_root=originals, dry_run=False)
    assert result["errors"] == 1 and src.exists()
    assert _row(conn, asset_id)["dest_path"] is None          # nothing recorded

    def recording(path):
        synced.append(path)
        real(path)

    monkeypatch.setattr(engine, "_fsync_dir", recording)
    result = engine.promote_rest(conn, originals_root=originals, dry_run=False)
    assert result["errors"] == 0 and not src.exists()
    dest_dir = originals / "2026" / "07"
    assert {dest_dir, dest_dir.parent, originals, originals.parent} <= set(synced)


def test_a_freshly_created_quarantine_root_is_synced_into_its_parent(tmp_path, monkeypatch):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    staged = _jpeg(tmp_path / "staging" / "photos" / "IMG_1.JPG", seed=21)
    _library_with(tmp_path, conn, staged)
    _register(conn, "photos", staged)
    engine.fingerprint_pending(conn)
    quarantine = tmp_path / "new" / "quarantine"          # neither exists yet
    real, synced = engine._fsync_dir, []
    monkeypatch.setattr(engine, "_fsync_dir", lambda p: (synced.append(p), real(p)))
    result = engine.apply_decisions(conn, originals_root=tmp_path / "originals",
                                    quarantine_root=quarantine, dry_run=False)
    assert result["aliases_quarantined"] == 1
    assert {quarantine, quarantine.parent, tmp_path} <= set(synced)


def test_index_library_dir_through_a_symlinked_ancestor_is_refused(tmp_path):
    from typer.testing import CliRunner

    from immy.cli import app

    lib = tmp_path / "originals"
    lib.mkdir()
    outside = tmp_path / "outside" / "subdir"
    outside.mkdir(parents=True)
    (lib / "link").symlink_to(tmp_path / "outside")
    (lib / "2026").mkdir()
    (lib / "2026" / "IMG_1.JPG").write_bytes(b"one")
    runner = CliRunner()
    bad = runner.invoke(app, ["dedup", "index-library", "--originals", str(lib),
                              "--dir", "link/subdir", "--manifest", str(tmp_path / "m.sqlite")])
    assert bad.exit_code == 1 and "symlink" in bad.output
    ok = runner.invoke(app, ["dedup", "index-library", "--originals", str(lib),
                             "--dir", "2026/../2026", "--manifest", str(tmp_path / "m.sqlite")])
    assert ok.exit_code == 0, ok.output
    from immy.dedup import manifest as m
    rows = m.open_manifest(tmp_path / "m.sqlite").execute("SELECT path FROM library_file").fetchall()
    assert rows == [(str(lib / "2026" / "IMG_1.JPG"),)]              # normalised, no '..'


def test_library_match_drops_a_row_reached_through_a_symlinked_directory(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / "a.jpg").write_bytes(b"bytes")
    (tmp_path / "originals").mkdir()
    linked = tmp_path / "originals" / "linked"
    linked.symlink_to(real_dir)
    path = linked / "a.jpg"
    manifest.upsert_library_file(conn, path, _sha(path), os.lstat(path))
    assert identity.library_match(conn, _sha(path)) is None
    assert conn.execute("SELECT COUNT(*) FROM library_file").fetchone()[0] == 0
