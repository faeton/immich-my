"""Tests for the per-trip phase journal and the resume path.

The journal is what makes `immy process` resumable across crashes /
Ctrl-C: each successful phase writes an entry keyed by
`(checksum_hex, worker, version)`, and the next run skips work whose
entry already exists at the current version.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from immy import journal as journal_mod
from immy import process as process_mod
from immy.journal import Journal
from immy.pg import LibraryInfo


FIXTURES = Path(__file__).parent / "fixtures"

LIB = LibraryInfo(
    id="lib-1",
    owner_id="owner-1",
    container_root="/mnt/external/originals",
)


# --- Journal unit tests --------------------------------------------------


def test_journal_load_returns_empty_when_missing(tmp_path: Path):
    j = Journal.load(tmp_path)
    assert j.entries == {}
    assert j.is_done("abc", "clip", "v1") is False


def test_journal_mark_and_query_roundtrip(tmp_path: Path):
    j = Journal.load(tmp_path)
    j.mark_done("cs1", "clip", "clip:mlx-clip", meta={"dim": 768})
    assert j.is_done("cs1", "clip", "clip:mlx-clip") is True
    assert j.is_done("cs1", "clip", "clip:other-model") is False
    assert j.is_done("cs1", "faces", "clip:mlx-clip") is False
    rec = j.get("cs1", "clip")
    assert rec["meta"]["dim"] == 768
    assert "completed_at" in rec


def test_journal_flush_persists_atomically(tmp_path: Path):
    j = Journal.load(tmp_path)
    j.mark_done("cs1", "ingest", "v1", meta={"asset_id": "uuid-x"})
    j.flush()
    # Reload from disk: same data.
    j2 = Journal.load(tmp_path)
    assert j2.is_done("cs1", "ingest", "v1") is True
    assert j2.get("cs1", "ingest")["meta"]["asset_id"] == "uuid-x"
    # No leftover .tmp files in the audit dir.
    leftovers = list((tmp_path / ".audit").glob("*.tmp"))
    assert leftovers == []


def test_journal_version_change_invalidates(tmp_path: Path):
    j = Journal.load(tmp_path)
    j.mark_done("cs1", "caption", "caption:gemma-4b")
    # Different model = different version → not done at the new version.
    assert j.is_done("cs1", "caption", "caption:gemma-27b") is False


def test_journal_clear_worker(tmp_path: Path):
    j = Journal.load(tmp_path)
    j.mark_done("cs1", "clip", "v1")
    j.mark_done("cs1", "faces", "v1")
    j.clear_worker("cs1", "clip")
    assert j.is_done("cs1", "clip", "v1") is False
    assert j.is_done("cs1", "faces", "v1") is True


def test_journal_load_tolerates_malformed_entries(tmp_path: Path):
    # A version-less or non-dict record should be silently dropped, not
    # crash the loader. A corrupted entry just re-runs its phase.
    audit = tmp_path / ".audit"
    audit.mkdir()
    (audit / journal_mod.JOURNAL_FILENAME).write_text(
        "schema: 1\n"
        "entries:\n"
        "  cs1:\n"
        "    clip: nonsense\n"
        "    faces: {version: v1, completed_at: 0}\n"
    )
    j = Journal.load(tmp_path)
    assert j.is_done("cs1", "clip", "v1") is False  # dropped
    assert j.is_done("cs1", "faces", "v1") is True


# --- Resume path: process_trip skips done work ---------------------------


def _make_conn(asset_uuid: str = "uuid-x") -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = (asset_uuid,)
    conn.cursor.return_value = cur
    return conn, cur


def test_process_trip_marks_ingest_in_journal(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn, _ = _make_conn()

    results = process_mod.process_trip(target, conn, LIB)

    assert len(results) == 1
    j = Journal.load(target)
    # The ingest entry uses the path-checksum hex as key.
    cs_hex = results[0].container_path  # rebuild from the result instead
    # Easier: just check journal has exactly one entry with an "ingest"
    # worker referencing the inserted asset id.
    assert len(j.entries) == 1
    only_entry = next(iter(j.entries.values()))
    assert "ingest" in only_entry
    # asset_id is the uuid build_rows minted (PgSink keeps it on success
    # — only conflict path replaces it with the existing-row id).
    assert only_entry["ingest"]["meta"]["asset_id"] == results[0].asset_id


def test_process_trip_skips_caption_when_journal_says_done(
    tmp_path: Path, monkeypatch
):
    """A pre-populated journal entry for the caption worker at the
    current model version must short-circuit the VLM call entirely.
    The dominant cost of overnight runs is captions (~9.5 s/image with
    Gemma); the journal is what makes resume cheap.
    """
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn, _ = _make_conn()

    # Stand in for derivatives so the caption gate downstream sees a
    # preview file. Captions don't strictly require a preview (they fall
    # back to in-memory pyvips resize), but the test stays focused.
    from immy.derivatives import DerivativeFile, DerivativeResult
    preview = tmp_path / "out" / "preview.jpeg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"fake")
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: DerivativeResult(
            files=[DerivativeFile(
                kind="preview", staged_path=preview,
                relative_path="thumbs/x/p.jpeg",
                is_progressive=True, is_transparent=False,
            )],
            width=100, height=100,
        ),
    )

    # Hard-fail if the captioner is called — the journal should prevent it.
    def _explode(*a, **kw):
        raise AssertionError("captions.caption was called despite journal hit")
    monkeypatch.setattr("immy.process.captions_mod.caption", _explode)

    from immy import captions as captions_mod
    cfg = captions_mod.CaptionerConfig(
        endpoint="http://x", model="gemma-3-4b", api_key=None,
        prompt="say something", max_tokens=64, timeout_s=10,
    )

    # Pre-load journal with a caption entry for the only media file's
    # checksum. Compute checksum the same way build_rows does.
    from immy import exif as exif_mod
    rows = exif_mod.read_folder(target)
    assert len(rows) == 1
    asset, _exif = process_mod.build_rows(rows[0].path, target, rows[0], LIB)
    cs_hex = asset.checksum.hex()

    j = Journal.load(target)
    j.mark_done(
        cs_hex, "ingest", "v1", meta={"asset_id": "uuid-x"},
    )
    j.mark_done(
        cs_hex, "caption", journal_mod.caption_version("gemma-3-4b"),
        meta={"text": "a sample image", "model": "gemma-3-4b",
              "prompt_tokens": 0, "completion_tokens": 0},
    )
    j.flush()

    # Run with captions on; the captioner must not be called.
    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=False, compute_faces=False,
        compute_captions=True, captioner_config=cfg,
    )
    assert len(results) == 1
    assert results[0].caption is not None
    assert results[0].caption.get("cached") is True
    assert results[0].caption["text"] == "a sample image"


def test_process_trip_recaption_ignores_journal(tmp_path: Path, monkeypatch):
    """`--recaption` must force the VLM call even if the journal has an
    entry at the current version. This is the user-facing escape hatch
    for "regenerate everything against the same model" (e.g. after a
    prompt template change)."""
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn, _ = _make_conn()

    from immy.derivatives import DerivativeFile, DerivativeResult
    preview = tmp_path / "out" / "preview.jpeg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"fake")
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: DerivativeResult(
            files=[DerivativeFile(
                kind="preview", staged_path=preview,
                relative_path="thumbs/x/p.jpeg",
                is_progressive=True, is_transparent=False,
            )],
            width=100, height=100,
        ),
    )

    called: dict = {"n": 0}
    def _fake_caption(media, *, config, preview=None):
        called["n"] += 1
        return MagicMock(text="fresh", model=config.model,
                         prompt_tokens=1, completion_tokens=2)
    monkeypatch.setattr("immy.process.captions_mod.caption", _fake_caption)
    # PgSink.get_description reads cur.fetchone() which the MagicMock
    # cursor populates with the asset-id sentinel — clear it for this
    # test so _process_caption sees an empty description (not "uuid-x")
    # and proceeds to the VLM call instead of bailing on the
    # "user-typed description" guard.
    from immy import offline as offline_mod
    monkeypatch.setattr(
        offline_mod.PgSink, "get_description",
        lambda self, asset_id: None,
    )

    from immy import captions as captions_mod
    cfg = captions_mod.CaptionerConfig(
        endpoint="http://x", model="gemma-3-4b", api_key=None,
        prompt="say", max_tokens=64, timeout_s=10,
    )

    from immy import exif as exif_mod
    rows = exif_mod.read_folder(target)
    asset, _exif = process_mod.build_rows(rows[0].path, target, rows[0], LIB)
    cs_hex = asset.checksum.hex()

    j = Journal.load(target)
    j.mark_done(cs_hex, "ingest", "v1", meta={"asset_id": "uuid-x"})
    j.mark_done(
        cs_hex, "caption", journal_mod.caption_version("gemma-3-4b"),
        meta={"text": "stale", "model": "gemma-3-4b",
              "prompt_tokens": 0, "completion_tokens": 0},
    )
    j.flush()

    process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=False, compute_faces=False,
        compute_captions=True, captioner_config=cfg, recaption=True,
    )
    assert called["n"] == 1


def test_process_trip_resumes_after_simulated_crash(tmp_path: Path, monkeypatch):
    """End-to-end resume: copy a 1-asset trip, run process once, simulate
    a crash by deleting only the DB-side state (mock conn), keep the
    journal + staged derivatives, run again, assert the second run skips
    derivatives via the journal-cached path.
    """
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn, _ = _make_conn()

    from immy.derivatives import DerivativeFile, DerivativeResult
    staged = target / ".audit" / "derivatives" / "preview.jpeg"
    staged.parent.mkdir(parents=True, exist_ok=True)

    call_count = {"n": 0}
    def _fake_compute(**kw):
        call_count["n"] += 1
        staged.write_bytes(b"fake preview")
        return DerivativeResult(
            files=[DerivativeFile(
                kind="preview", staged_path=staged,
                relative_path="thumbs/x/p.jpeg",
                is_progressive=True, is_transparent=False,
            )],
            width=100, height=100,
        )
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset", _fake_compute,
    )

    # First pass: derivatives compute runs, journal records it.
    process_mod.process_trip(
        target, conn, LIB, compute_derivatives=True,
        compute_clip=False, compute_faces=False,
    )
    assert call_count["n"] == 1

    # Second pass with a fresh mock conn: derivatives must NOT recompute
    # because the journal says done at DERIVATIVES_VERSION and the
    # staged file still exists.
    conn2, cur2 = _make_conn(asset_uuid="uuid-x")
    process_mod.process_trip(
        target, conn2, LIB, compute_derivatives=True,
        compute_clip=False, compute_faces=False,
    )
    assert call_count["n"] == 1, "derivatives recomputed despite journal hit"


def test_process_trip_reruns_derivatives_when_staged_files_missing(
    tmp_path: Path, monkeypatch,
):
    """If the journal says derivatives are done but the staged files were
    wiped from `.audit/derivatives/`, fall through and recompute. The
    journal isn't allowed to lie about disk state."""
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn, _ = _make_conn()

    from immy.derivatives import DerivativeFile, DerivativeResult
    staged = target / ".audit" / "derivatives" / "preview.jpeg"
    staged.parent.mkdir(parents=True, exist_ok=True)

    call_count = {"n": 0}
    def _fake_compute(**kw):
        call_count["n"] += 1
        staged.write_bytes(b"fake")
        return DerivativeResult(
            files=[DerivativeFile(
                kind="preview", staged_path=staged,
                relative_path="thumbs/x/p.jpeg",
                is_progressive=True, is_transparent=False,
            )],
            width=100, height=100,
        )
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset", _fake_compute,
    )

    process_mod.process_trip(
        target, conn, LIB, compute_derivatives=True,
        compute_clip=False, compute_faces=False,
    )
    # Wipe the staged dir but keep the journal.
    staged.unlink()

    conn2, _ = _make_conn(asset_uuid="uuid-x")
    process_mod.process_trip(
        target, conn2, LIB, compute_derivatives=True,
        compute_clip=False, compute_faces=False,
    )
    assert call_count["n"] == 2, "derivatives should re-run when files wiped"
