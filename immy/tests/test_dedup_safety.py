"""Dedup safety guards — the four correctness bugs found 2026-09-18.

Every test here pins a path where the cascade was willing to delete or
quarantine a file on evidence that does not actually identify it. They are
deliberately kept together: the common thread is "size, filename or a stale
score stood in for content", and the fixes are only meaningful as a set.

    P0.1  _resolve_dest          same-size stranger at the destination
    P0.2  video exact-match      same-size, same-stem, different recording
    P0.3  extended cluster       CLIP score earned by other members
    P0.4  RAW/JPEG companions    exclusion lost to transitive clustering

No pyvips here — none of these need a decode, so the whole file runs on any
host, including the NAS container.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from immy.dedup import engine, manifest


# ------------------------------------------------------------------ helpers


def _asset(
    id: int,
    *,
    source: str = "icloud",
    path: str = "/staging/IMG_0001.HEIC",
    bytes: int = 4_000_000,
    media_type: str = "image",
    format: str = "heic",
    width: int | None = 4032,
    height: int | None = 3024,
    taken_at: str | None = "2025-06-01T12:00:00",
    taken_src: str | None = "exif",
    phash_value: int | None = 0xAAAA5555AAAA5555,
    live_cid: str | None = None,
) -> engine.AssetLite:
    return engine.AssetLite(
        id=id, source=source, path=path, bytes=bytes, media_type=media_type,
        format=format, width=width, height=height, taken_at=taken_at,
        taken_src=taken_src, gps_lat=None, gps_lon=None, phash=phash_value,
        exif_fields=40, burst_uuid=None, live_cid=live_cid, edited=False,
    )


def _video_file(path: Path, *, size: int, fill: bytes) -> Path:
    """A file of exactly `size` bytes whose content is `fill` repeated —
    two calls with the same size and different fills are the collision this
    module is about."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((fill * (size // len(fill) + 1))[:size])
    return path


# ------------------------------------------------- P0.1  content_equal


def test_content_equal_distinguishes_same_size_files(tmp_path):
    a = _video_file(tmp_path / "a.mov", size=5000, fill=b"aaaa")
    b = _video_file(tmp_path / "b.mov", size=5000, fill=b"bbbb")
    c = _video_file(tmp_path / "c.mov", size=5000, fill=b"aaaa")
    assert a.stat().st_size == b.stat().st_size  # the trap
    assert engine.content_equal(a, b) is False
    assert engine.content_equal(a, c) is True


def test_content_equal_samples_large_files(tmp_path, monkeypatch):
    """Above the full-compare ceiling only three windows are read — the
    tail window is what catches a file that diverges only at the end."""
    monkeypatch.setattr(engine, "CONTENT_FULL_COMPARE_MAX", 1024)
    monkeypatch.setattr(engine, "CONTENT_SAMPLE_WINDOW", 256)
    body = b"x" * 4096
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(body)
    b.write_bytes(body[:-1] + b"y")
    assert engine.content_equal(a, a) is True
    assert engine.content_equal(a, b) is False


def test_content_equal_or_unknown_is_none_when_unreadable(tmp_path):
    present = _video_file(tmp_path / "here.mov", size=100, fill=b"a")
    assert engine._content_equal_or_unknown(present, tmp_path / "gone.mov") is None


# ------------------------------------------------- P0.1  _resolve_dest


def test_resolve_dest_same_size_stranger_is_a_collision(tmp_path):
    """The bug, minimally: the destination is occupied by a DIFFERENT asset
    that happens to share a byte length. Size alone said "already done", and
    the caller then deleted the staging file without copying it anywhere."""
    src = _video_file(tmp_path / "staging" / "IMG_1234.JPG", size=2048, fill=b"new-")
    dest = _video_file(tmp_path / "originals" / "IMG_1234.JPG", size=2048, fill=b"old-")

    resolved, already_done = engine._resolve_dest(dest, 77, 2048, src)
    assert already_done is False
    assert resolved == dest.with_name("IMG_1234__77.JPG")


def test_resolve_dest_recognizes_its_own_finished_copy(tmp_path):
    """The case the size check existed for: a prior run copied the file and
    died before unlinking the source. Same size AND same bytes → resume."""
    src = _video_file(tmp_path / "staging" / "IMG_1234.JPG", size=2048, fill=b"same")
    dest = _video_file(tmp_path / "originals" / "IMG_1234.JPG", size=2048, fill=b"same")

    assert engine._resolve_dest(dest, 77, 2048, src) == (dest, True)


def test_resolve_dest_checks_content_at_the_qualified_name_too(tmp_path):
    src = _video_file(tmp_path / "staging" / "IMG_1234.JPG", size=2048, fill=b"mine")
    dest = _video_file(tmp_path / "originals" / "IMG_1234.JPG", size=2048, fill=b"othr")
    qualified = dest.with_name("IMG_1234__77.JPG")

    # qualified holds this asset's own earlier copy → resume there
    _video_file(qualified, size=2048, fill=b"mine")
    assert engine._resolve_dest(dest, 77, 2048, src) == (qualified, True)

    # qualified holds yet another same-size stranger → refuse to guess
    _video_file(qualified, size=2048, fill=b"3rd-")
    with pytest.raises(FileExistsError):
        engine._resolve_dest(dest, 77, 2048, src)


def test_resolve_dest_falls_back_to_size_when_src_is_gone(tmp_path):
    """Copy AND unlink finished, status commit didn't. Nothing is left to
    compare and nothing is deleted on this path, so the recorded size is
    allowed to settle it — otherwise the pipeline could never resume."""
    dest = _video_file(tmp_path / "originals" / "IMG_1234.JPG", size=2048, fill=b"gone")
    missing_src = tmp_path / "staging" / "IMG_1234.JPG"
    assert engine._resolve_dest(dest, 77, 2048, missing_src) == (dest, True)


def _manifest_with_staging_asset(tmp_path, *, status, size, fill) -> tuple:
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    src = _video_file(tmp_path / "staging" / "IMG_1234.JPG", size=size, fill=fill)
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, taken_at, media_type)"
        " VALUES (1, 'icloud', ?, ?, ?, '2024-06-15T10:00:00', 'image')",
        (str(src), status, size),
    )
    conn.commit()
    return conn, src


def test_promote_rest_never_drops_an_asset_onto_a_same_size_stranger(tmp_path):
    """End to end: the library already holds an unrelated 2024/06/IMG_1234.JPG
    of equal length. Before the fix this promoted zero bytes and unlinked the
    staging file; the asset was recorded `promoted` and was simply gone."""
    conn, src = _manifest_with_staging_asset(
        tmp_path, status=manifest.FINGERPRINTED, size=2048, fill=b"new-"
    )
    originals = tmp_path / "originals"
    stranger = _video_file(originals / "2024" / "06" / "IMG_1234.JPG",
                           size=2048, fill=b"old-")
    mine = src.read_bytes()

    result = engine.promote_rest(conn, originals_root=originals, dry_run=False)

    assert result == {**result, "promoted": 1, "errors": 0}
    assert stranger.read_bytes() != mine          # the incumbent is untouched
    landed = originals / "2024" / "06" / "IMG_1234__1.JPG"
    assert landed.read_bytes() == mine            # and this asset really landed
    assert not src.exists()
    assert conn.execute("SELECT status FROM asset WHERE id=1").fetchone()[0] == (
        manifest.PROMOTED
    )


def test_apply_decisions_quarantines_a_loser_onto_a_qualified_name(tmp_path):
    """Same guarantee on the quarantine side — `_quarantine_dest`'s own
    docstring warns its fallback can collide, and a loser is a file too."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    originals, quarantine = tmp_path / "originals", tmp_path / "quarantine"
    winner = _video_file(tmp_path / "staging" / "a" / "IMG_9.JPG", size=1024, fill=b"win-")
    loser = _video_file(tmp_path / "staging" / "b" / "IMG_9.JPG", size=1024, fill=b"lose")
    conn.execute("INSERT INTO cluster (id, decision, winner_asset_id) "
                 "VALUES (1, 'auto', 1)")
    for asset_id, path in ((1, winner), (2, loser)):
        conn.execute(
            "INSERT INTO asset (id, source, path, status, bytes, taken_at, media_type)"
            " VALUES (?, 'icloud', ?, ?, 1024, '2024-06-15T10:00:00', 'image')",
            (asset_id, str(path), manifest.DECIDED),
        )
        conn.execute("INSERT INTO membership (cluster_id, asset_id) VALUES (1, ?)",
                     (asset_id,))
    conn.commit()
    # A same-size stranger already sitting where the loser would land.
    # These staging paths are not under /staging, so `_quarantine_dest`
    # takes its basename-only fallback — the very branch its docstring
    # warns "CAN collide", which is what makes this the realistic case.
    stranger = _video_file(quarantine / "IMG_9.JPG", size=1024, fill=b"strn")
    loser_bytes = loser.read_bytes()

    result = engine.apply_decisions(
        conn, originals_root=originals, quarantine_root=quarantine, dry_run=False
    )

    assert (result["promoted"], result["quarantined"], result["errors"]) == (1, 1, 0)
    assert stranger.read_bytes() == b"strn" * 256
    assert (quarantine / "IMG_9__2.JPG").read_bytes() == loser_bytes


# --------------------------------------------- P0.2  video exact matching


def _video_pair(tmp_path, *, same_content: bool) -> tuple:
    a = _video_file(tmp_path / "a" / "IMG_1234.MOV", size=4096, fill=b"aaaa")
    b = _video_file(tmp_path / "b" / "IMG_1234.MOV", size=4096,
                    fill=b"aaaa" if same_content else b"bbbb")
    return a, b


def _video_asset(id: int, path: Path, **kwargs) -> engine.AssetLite:
    kwargs.setdefault("media_type", "video")
    kwargs.setdefault("format", "mov")
    kwargs.setdefault("phash_value", None)
    kwargs.setdefault("bytes", path.stat().st_size)
    return _asset(id, path=str(path), **kwargs)


def test_pair_evidence_video_equal_size_but_different_content_is_not_strong(tmp_path):
    """Two unrelated IMG_1234.MOV of identical length, years apart. The
    equal-size early return called this `strong` and skipped the date
    plausibility gate entirely — Live Photo .mov halves make equal lengths
    common enough that this is not a thought experiment."""
    a_path, b_path = _video_pair(tmp_path, same_content=False)
    a = _video_asset(1, a_path, taken_at="2011-01-01T00:00:00", taken_src="exif")
    b = _video_asset(2, b_path, taken_at="2026-06-20T00:00:00", taken_src="exif")
    assert engine._pair_evidence(a, b) is None


def test_pair_evidence_video_identical_content_is_still_strong(tmp_path):
    a_path, b_path = _video_pair(tmp_path, same_content=True)
    a = _video_asset(1, a_path, taken_at="2011-01-01T00:00:00", taken_src="mtime")
    b = _video_asset(2, b_path, taken_at="2026-06-20T00:00:00", taken_src="mtime")
    assert engine._pair_evidence(a, b) == ("strong", None)


def test_pair_evidence_video_unreadable_file_falls_back_to_dates(tmp_path):
    """An offline mount must not turn into evidence either way: unverifiable
    content drops through to the date gate, which is what pairs these."""
    a = _video_asset(1, _video_file(tmp_path / "a.mov", size=10, fill=b"a"),
                     taken_at="2025-06-10T09:00:00", taken_src="exif")
    b = _asset(2, path=str(tmp_path / "never-existed.mov"), bytes=10,
               media_type="video", format="mov", phash_value=None,
               taken_at="2025-06-10T09:04:00", taken_src="exif")
    assert engine._pair_evidence(a, b) == ("candidate", None)


def test_decide_one_video_requires_identical_content(tmp_path):
    a_path, b_path = _video_pair(tmp_path, same_content=False)
    winner = _video_asset(1, a_path, source="icloud")
    member = _video_asset(2, b_path, source="google")
    # same stem, same size, same timestamp: every pre-fix signal agrees
    assert engine._decide_one([winner, member]) == "review"

    same_a, same_b = _video_pair(tmp_path / "twins", same_content=True)
    assert engine._decide_one([
        _video_asset(1, same_a, source="icloud"),
        _video_asset(2, same_b, source="google"),
    ]) == "auto"


def test_decide_one_conflicting_live_cid_is_never_auto(tmp_path):
    """Two members that each name a capture, and name different ones. A
    ContentIdentifier is identity, not a similarity score — it outranks
    whatever pHash and the filename agree on."""
    a = _asset(1, source="icloud", live_cid="AAAA-1111")
    b = _asset(2, source="google", live_cid="BBBB-2222")
    assert engine._decide_one([a, b]) == "review"
    # one side unknown is not a conflict — Takeout drops the field
    assert engine._decide_one([a, _asset(2, source="google")]) == "auto"


# ------------------------------------- P0.3  stale CLIP score on extension


def _insert_image(conn, asset_id, *, source, path, status, phash="aaaa5555aaaa5555"):
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, media_type, format,"
        " width, height, taken_at, taken_src, phash, exif_fields)"
        " VALUES (?, ?, ?, ?, 4000000, 'image', 'heic', 4032, 3024,"
        " '2025-06-01T12:00:00', 'exif', ?, 40)",
        (asset_id, source, path, status, phash),
    )


def _clustered_pair(tmp_path) -> "object":
    """A settled, CLIP-scored cluster 1 holding a canonical library asset and
    the staging copy it matched. The canonical member is what `cluster()`
    keeps re-admitting (`universe` deliberately never drops `originals`
    rows), so it is the bridge a later arrival attaches through."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    conn.execute(
        "INSERT INTO cluster (id, decision, clip_cos_sim) VALUES (1, 'review', 0.999)"
    )
    _insert_image(conn, 1, source="originals", path="/library/IMG_1001.HEIC",
                  status=manifest.CANONICAL)
    _insert_image(conn, 2, source="icloud", path="/staging/IMG_1001.HEIC",
                  status=manifest.CLUSTERED)
    for asset_id in (1, 2):
        conn.execute("INSERT INTO membership (cluster_id, asset_id) VALUES (1, ?)",
                     (asset_id,))
    conn.commit()
    return conn


def test_cluster_extension_clears_the_inherited_clip_score(tmp_path):
    """A new arrival joins a settled cluster. Its cos_sim was earned by two
    OTHER images, and `_clip_ready_clusters` only ever looks at clusters
    where it is NULL — so the newcomer inherited a 0.999 that was never
    about it and `decide()` auto-merged it away."""
    conn = _clustered_pair(tmp_path)
    _insert_image(conn, 3, source="google", path="/takeout/IMG_1001.HEIC",
                  status=manifest.FINGERPRINTED, phash="aaaa5555aaa05555")
    conn.commit()

    result = engine.cluster(conn)

    assert result["clusters_extended"] == 1
    assert conn.execute(
        "SELECT clip_cos_sim FROM cluster WHERE id=1"
    ).fetchone()[0] is None
    assert {r[0] for r in conn.execute(
        "SELECT asset_id FROM membership WHERE cluster_id=1"
    )} == {1, 2, 3}


def test_cluster_rerun_without_new_members_keeps_its_clip_score(tmp_path):
    """The invalidation is membership-driven, not run-driven: an idempotent
    re-cluster must not throw away work Stage C already paid for."""
    conn = _clustered_pair(tmp_path)
    result = engine.cluster(conn)
    assert result["clusters_extended"] == 0
    assert conn.execute(
        "SELECT clip_cos_sim FROM cluster WHERE id=1"
    ).fetchone()[0] == 0.999


def test_cleared_clip_score_routes_the_cluster_to_review():
    """Why clearing it is safe: with no score, the Stage C shortcut is
    unavailable and a non-strong pHash pair falls to review, which is where
    an unscored cluster belongs."""
    winner = _asset(1, phash_value=0xAAAA5555AAAA5555)
    member = _asset(2, source="google", phash_value=0xAAAA5555AAAA0000)
    assert engine._decide_one([winner, member], clip_cos=0.999) == "auto"
    assert engine._decide_one([winner, member], clip_cos=None) == "review"


# ------------------------------- P0.4  RAW/JPEG companions under transitivity


def test_decide_one_spares_a_raw_jpeg_pair_joined_through_a_third_image():
    """`_pair_evidence` suppresses the direct RAW↔JPEG edge, but union-find
    still joins both through a third image that matches each. Same aspect
    ratio clears the crop guard, differing dimensions clear the burst guard
    — and both Photos components, RAW included, became losers."""
    dng = _asset(1, source="icloud", path="/staging/IMG_1234.DNG", format="dng",
                 width=6000, height=4000)
    jpg = _asset(2, source="icloud", path="/staging/IMG_1234.JPG", format="jpg",
                 width=6000, height=4000)
    canonical = _asset(3, source="google", path="/takeout/IMG_1234.JPG",
                       format="jpg", width=3000, height=2000)

    assert engine._pair_evidence(dng, jpg) is None      # direct edge still barred
    assert engine._pair_evidence(dng, canonical) is not None  # …but these pair
    assert engine._pair_evidence(jpg, canonical) is not None
    assert engine._decide_one([dng, jpg, canonical]) == "review"


def test_decide_one_still_auto_merges_a_cluster_without_companions():
    """Control: the guard is about companion pairs, not about RAW being
    present. Three plain re-exports still auto-merge."""
    members = [
        _asset(1, source="icloud", path="/staging/IMG_1234.HEIC", format="heic",
               width=6000, height=4000),
        _asset(2, source="google", path="/takeout/IMG_1234.JPG", format="jpg",
               width=6000, height=4000),
        # a smaller re-export: differing dimensions keep the burst guard
        # (>=3 shots within 1s at identical dims) out of the way
        _asset(3, source="google", path="/takeout2/IMG_1234.JPG", format="jpg",
               width=3000, height=2000),
    ]
    assert engine._decide_one(members) == "auto"


def test_decide_one_ignores_same_stem_raw_jpeg_in_different_directories():
    """A companion pair is same-directory by definition — two unrelated trips
    that both reached DJI_0655 are a normal dupe question, not a companion."""
    dng = _asset(1, source="icloud", path="/tripA/DJI_0655.DNG", format="dng")
    jpg = _asset(2, source="google", path="/tripB/DJI_0655.JPG", format="jpg")
    assert engine._decide_one([dng, jpg]) == "auto"
