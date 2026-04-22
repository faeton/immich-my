"""Tests for the `find-duplicates` scanner.

Covers the four verdicts, the three hash modes, the walker's ignore / bundle
rules, and the Markdown/JSON rendering. Fixtures are plain files on disk —
cheap and they exercise the real `sha1_of` implementation.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from immy import duplicates as dup
from immy import snapshot as snap


def _seed_snapshot(path: Path, rows: list[snap.AssetRow]) -> None:
    db = snap.create(path)
    snap.write_rows(db, rows)
    db.close()


def _row(asset_id: str, filename: str, size: int,
         checksum: bytes | None = None) -> snap.AssetRow:
    return snap.AssetRow(
        asset_id=asset_id, filename=filename, size_bytes=size,
        checksum=checksum, taken_at=None, asset_type="IMAGE",
        library_id=None,
    )


def _write(p: Path, content: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


# --- walker ---------------------------------------------------------------


def test_walker_skips_ignore_globs(tmp_path: Path) -> None:
    _write(tmp_path / "keep.jpg", b"x")
    _write(tmp_path / ".DS_Store", b"x")
    _write(tmp_path / "Thumbs.db", b"x")
    _write(tmp_path / "._hidden", b"x")
    files = {p.name for p in dup.iter_candidate_files(tmp_path)}
    assert files == {"keep.jpg"}


def test_walker_skips_bundles_by_default(tmp_path: Path) -> None:
    _write(tmp_path / "normal.jpg", b"x")
    _write(tmp_path / "Photos Library.photoslibrary" / "inside.jpg", b"x")
    files = {p.name for p in dup.iter_candidate_files(tmp_path)}
    assert files == {"normal.jpg"}


def test_walker_into_bundles_descends(tmp_path: Path) -> None:
    _write(tmp_path / "X.photoslibrary" / "inside.jpg", b"x")
    files = {p.name for p in dup.iter_candidate_files(tmp_path, into_bundles=True)}
    assert files == {"inside.jpg"}


def test_walker_respects_min_size(tmp_path: Path) -> None:
    _write(tmp_path / "tiny.jpg", b"x")      # 1 byte
    _write(tmp_path / "big.jpg", b"x" * 1000)
    files = {p.name for p in dup.iter_candidate_files(tmp_path, min_size=100)}
    assert files == {"big.jpg"}


def test_walker_ignores_symlinks_by_default(tmp_path: Path) -> None:
    target = tmp_path / "target.jpg"
    _write(target, b"x")
    link = tmp_path / "link.jpg"
    os.symlink(target, link)
    files = {p.name for p in dup.iter_candidate_files(tmp_path)}
    # The real file is yielded; the symlink is skipped.
    assert files == {"target.jpg"}


# --- classify_one: the four verdicts -------------------------------------


def test_exact_match_verified_by_sha1(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    body = b"photo bytes"
    sha = hashlib.sha1(body).digest()
    _seed_snapshot(snap_path, [_row("a", "photo.jpg", len(body), sha)])

    local = tmp_path / "scan" / "photo.jpg"
    _write(local, body)

    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(local, db)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.EXACT
    assert r.matched_asset_id == "a"


def test_likely_when_fast_mode_skips_hash(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    body = b"photo bytes"
    sha = hashlib.sha1(body).digest()
    _seed_snapshot(snap_path, [_row("a", "photo.jpg", len(body), sha)])

    local = tmp_path / "scan" / "photo.jpg"
    _write(local, body)

    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(local, db, hash_mode=dup.HashMode.FAST)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.LIKELY


def test_likely_when_snapshot_checksum_missing(tmp_path: Path) -> None:
    # Snapshot came from a pre-exif Immich state where checksum wasn't
    # populated. Name+size match is the best we can do.
    snap_path = tmp_path / "snap.sqlite"
    body = b"photo bytes"
    _seed_snapshot(snap_path, [_row("a", "photo.jpg", len(body), None)])
    local = tmp_path / "scan" / "photo.jpg"
    _write(local, body)

    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(local, db)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.LIKELY


def test_name_only_when_size_differs(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _seed_snapshot(snap_path, [_row("a", "photo.jpg", 9_999_999, b"\xaa" * 20)])
    local = tmp_path / "scan" / "photo.jpg"
    _write(local, b"different")

    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(local, db)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.NAME_ONLY
    assert r.matched_asset_id == "a"


def test_name_only_when_bytes_differ_at_same_size(tmp_path: Path) -> None:
    # Evil edge case: same name, same size, different bytes. Should NOT be
    # exact, and also not silently collapse to likely — surface for review.
    snap_path = tmp_path / "snap.sqlite"
    snapshot_body = b"snapshot content!"
    snapshot_sha = hashlib.sha1(snapshot_body).digest()
    _seed_snapshot(snap_path, [_row("a", "photo.jpg", len(snapshot_body), snapshot_sha)])
    local = tmp_path / "scan" / "photo.jpg"
    local_body = b"different, same len"[: len(snapshot_body)]
    # Pad to exactly same length.
    local_body = local_body + b"x" * (len(snapshot_body) - len(local_body))
    assert len(local_body) == len(snapshot_body)
    _write(local, local_body)

    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(local, db)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.NAME_ONLY


def test_no_match(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _seed_snapshot(snap_path, [_row("a", "other.jpg", 500)])
    local = tmp_path / "scan" / "new.jpg"
    _write(local, b"some bytes")

    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(local, db)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.NO_MATCH


def test_thorough_finds_renamed_file(tmp_path: Path) -> None:
    # Same bytes, different name → thorough mode catches it as EXACT via
    # checksum. Without --thorough it lands as NO_MATCH (and that's fine).
    snap_path = tmp_path / "snap.sqlite"
    body = b"the original bytes"
    sha = hashlib.sha1(body).digest()
    _seed_snapshot(snap_path, [_row("a", "original.jpg", len(body), sha)])

    local = tmp_path / "scan" / "renamed.jpg"
    _write(local, body)

    db = snap.open_for_read(snap_path)
    try:
        on_match = dup.classify_one(local, db, hash_mode=dup.HashMode.ON_MATCH)
        thorough = dup.classify_one(local, db, hash_mode=dup.HashMode.THOROUGH)
    finally:
        db.close()
    assert on_match.verdict == dup.Verdict.NO_MATCH
    assert thorough.verdict == dup.Verdict.EXACT
    assert thorough.matched_asset_id == "a"


# --- scan: end-to-end -----------------------------------------------------


def test_scan_end_to_end(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    exact_body = b"exact match bytes"
    likely_body = b"likely body no cs"
    _seed_snapshot(snap_path, [
        _row("a", "exact.jpg", len(exact_body), hashlib.sha1(exact_body).digest()),
        _row("b", "likely.jpg", len(likely_body), None),  # no checksum → likely
        _row("c", "has-name.jpg", 9999, b"\xcc" * 20),
    ])

    root = tmp_path / "scan"
    _write(root / "exact.jpg", exact_body)
    _write(root / "likely.jpg", likely_body)
    _write(root / "has-name.jpg", b"wrong-size")  # name-only
    _write(root / "brand-new.jpg", b"genuinely new")  # no-match
    _write(root / ".DS_Store", b"junk")

    summary = dup.scan(root, snap_path)
    assert summary.files_scanned == 4
    assert summary.count(dup.Verdict.EXACT) == 1
    assert summary.count(dup.Verdict.LIKELY) == 1
    assert summary.count(dup.Verdict.NAME_ONLY) == 1
    assert summary.count(dup.Verdict.NO_MATCH) == 1


def test_scan_empty_directory(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _seed_snapshot(snap_path, [])
    root = tmp_path / "empty"
    root.mkdir()
    summary = dup.scan(root, snap_path)
    assert summary.files_scanned == 0
    assert summary.bytes_scanned == 0


def test_scan_progress_callback_fires(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _seed_snapshot(snap_path, [])
    root = tmp_path / "scan"
    for i in range(5):
        _write(root / f"f{i}.jpg", b"x")
    seen: list[Path] = []
    dup.scan(root, snap_path, progress=lambda p, r: seen.append(p))
    assert len(seen) == 5


# --- rendering ------------------------------------------------------------


def test_render_markdown_includes_summary_and_paths(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    body = b"exact"
    _seed_snapshot(snap_path, [
        _row("a", "exact.jpg", len(body), hashlib.sha1(body).digest()),
    ])
    root = tmp_path / "scan"
    _write(root / "exact.jpg", body)
    _write(root / "new.jpg", b"new")

    summary = dup.scan(root, snap_path)
    md = dup.render_markdown(summary, root)
    assert "Duplicate scan report" in md
    assert "exact.jpg" in md
    assert "new.jpg" in md
    # Summary table has all four verdicts even if some are 0.
    for v in dup.Verdict:
        assert v.value in md


def test_to_json_rows_stable_shape(tmp_path: Path) -> None:
    snap_path = tmp_path / "snap.sqlite"
    _seed_snapshot(snap_path, [])
    root = tmp_path / "scan"
    _write(root / "f.jpg", b"x")
    summary = dup.scan(root, snap_path)
    rows = dup.to_json_rows(summary)
    assert rows == [{
        "path": str(root / "f.jpg"),
        "size_bytes": 1,
        "verdict": "no-match",
        "matched_asset_id": None,
        "matched_filename": None,
        "matched_size": None,
    }]


# --- edge cases -----------------------------------------------------------


def test_classify_handles_file_that_disappeared(tmp_path: Path) -> None:
    # If a file vanishes between walk and stat we should not crash — treat
    # as no-match. We simulate by passing a path that doesn't exist.
    snap_path = tmp_path / "snap.sqlite"
    _seed_snapshot(snap_path, [])
    db = snap.open_for_read(snap_path)
    try:
        r = dup.classify_one(tmp_path / "ghost.jpg", db)
    finally:
        db.close()
    assert r.verdict == dup.Verdict.NO_MATCH
    assert r.size_bytes == 0


def test_human_bytes_formats_expected_scales() -> None:
    assert dup._human_bytes(0) == "0 B"
    assert dup._human_bytes(1023) == "1023 B"
    assert dup._human_bytes(1024).endswith("KB")
    assert dup._human_bytes(5 * 1024 * 1024).endswith("MB")
    assert dup._human_bytes(3 * 1024 * 1024 * 1024).endswith("GB")
