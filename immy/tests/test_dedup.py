"""Dedup cascade core: pHash, manifest lifecycle, blocking, deciding.

Real-image pHash behaviour (downscale survives, distinct images differ) is
exercised with generated JPEGs via pyvips — structured gradients + shapes,
not random noise, since pHash of noise is meaningless.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from immy.dedup import engine, manifest, phash


# ------------------------------------------------------------------ helpers


def _write_test_jpeg(path: Path, *, seed: int, side: int = 512) -> None:
    """A deterministic structured image: gradient + seed-placed rectangles."""
    import pyvips

    rng = np.random.default_rng(seed)
    ramp = np.linspace(0, 255, side, dtype=np.float64)
    pixels = np.tile(ramp, (side, 1))
    for _ in range(6):
        x, y = rng.integers(0, side - 64, size=2)
        w, h = rng.integers(32, 128, size=2)
        pixels[y:y + h, x:x + w] = rng.integers(0, 255)
    image = pyvips.Image.new_from_memory(
        pixels.astype(np.uint8).tobytes(), side, side, 1, "uchar"
    )
    image.jpegsave(str(path), Q=90)


def _asset(
    id: int,
    *,
    source: str = "icloud",
    path: str = "IMG_0001.HEIC",
    bytes: int = 4_000_000,
    media_type: str = "image",
    format: str = "heic",
    width: int | None = 4032,
    height: int | None = 3024,
    taken_at: str | None = "2025-06-01T12:00:00",
    taken_src: str | None = "exif",
    gps: tuple[float, float] | None = None,
    phash_value: int | None = 0xAAAA5555AAAA5555,
    exif_fields: int = 40,
    burst_uuid: str | None = None,
    live_cid: str | None = None,
    edited: bool = False,
) -> engine.AssetLite:
    return engine.AssetLite(
        id=id, source=source, path=path, bytes=bytes, media_type=media_type,
        format=format, width=width, height=height, taken_at=taken_at,
        taken_src=taken_src,
        gps_lat=gps[0] if gps else None, gps_lon=gps[1] if gps else None,
        phash=phash_value, exif_fields=exif_fields,
        burst_uuid=burst_uuid, live_cid=live_cid, edited=edited,
    )


# -------------------------------------------------------------------- phash


def test_hamming():
    assert phash.hamming(0, 0) == 0
    assert phash.hamming(0b1011, 0b0010) == 2
    assert phash.hamming(0, (1 << 64) - 1) == 64


def test_phash_hex_roundtrip():
    value = 0x0123456789ABCDEF
    assert phash.from_hex(phash.to_hex(value)) == value
    assert len(phash.to_hex(0)) == 16


def test_phash_survives_downscale(tmp_path):
    import pyvips

    original = tmp_path / "orig.jpg"
    _write_test_jpeg(original, seed=7)
    small = tmp_path / "small.jpg"
    pyvips.Image.thumbnail(str(original), 256).jpegsave(str(small), Q=80)

    distance = phash.hamming(phash.phash_file(original), phash.phash_file(small))
    assert distance <= engine.HAMMING_STRONG


def test_phash_separates_distinct_images(tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    _write_test_jpeg(a, seed=1)
    _write_test_jpeg(b, seed=2)
    distance = phash.hamming(phash.phash_file(a), phash.phash_file(b))
    assert distance > engine.HAMMING_CANDIDATE


# ------------------------------------------------------------------ manifest


def test_manifest_register_and_watermark(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    (root / "IMG_0001.HEIC").write_bytes(b"x" * 100)
    (root / "IMG_0002.JPG").write_bytes(b"y" * 200)
    (root / "meta.json").write_text("{}")  # sidecar, not an asset
    (root / "notes.txt").write_text("no")

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    result = manifest.register(conn, "icloud", root)
    assert result.new == 2 and result.already_known == 0

    again = manifest.register(conn, "icloud", root)
    assert again.new == 0 and again.already_known == 2

    watermark = manifest.get_meta(conn, manifest.watermark_key("icloud"))
    assert watermark is not None and float(watermark) > 0


def test_manifest_min_age_gate(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    fresh = root / "IMG_0003.HEIC"
    fresh.write_bytes(b"z" * 100)  # mtime = now → younger than any gate

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    result = manifest.register(conn, "icloud", root, min_age_hours=1.0)
    assert result.new == 0 and result.skipped_young == 1


def test_manifest_fingerprint_whitelist(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    root = tmp_path / "s"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"j")
    manifest.register(conn, "google", root)
    (asset_id, _, _), = manifest.pending_fingerprint(conn)

    with pytest.raises(ValueError):
        manifest.write_fingerprint(conn, asset_id, {"nope": 1})

    manifest.write_fingerprint(conn, asset_id, {"media_type": "image", "phash": "ff" * 8})
    assert manifest.pending_fingerprint(conn) == []


def test_manifest_migration_adds_clip_cos_sim_column(tmp_path):
    """Simulate a v1 manifest (predates `cluster.clip_cos_sim`) and confirm
    `open_manifest` migrates it in place — n5's live manifest is exactly
    this case: schema grows, existing rows don't move."""
    path = tmp_path / "m.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE cluster (
          id INTEGER PRIMARY KEY, winner_asset_id INTEGER, confidence REAL,
          decision TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '1');
        """
    )
    conn.commit()
    conn.close()

    reopened = manifest.open_manifest(path)
    cols = {row[1] for row in reopened.execute("PRAGMA table_info(cluster)")}
    assert "clip_cos_sim" in cols
    assert manifest.get_meta(reopened, "schema_version") == "2"


def test_manifest_embedding_roundtrip(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    root = tmp_path / "s"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"j")
    manifest.register(conn, "icloud", root)
    (asset_id,) = conn.execute("SELECT id FROM asset LIMIT 1").fetchone()

    assert manifest.get_embedding(conn, asset_id, "ViT-B-32__openai") is None
    manifest.set_embedding(conn, asset_id, "ViT-B-32__openai", [0.1, 0.2, 0.3])
    conn.commit()

    vec = manifest.get_embedding(conn, asset_id, "ViT-B-32__openai")
    assert vec is not None and len(vec) == 3
    assert abs(vec[0] - 0.1) < 1e-5

    # A different model name is a cache miss — embeddings aren't comparable
    # across models.
    assert manifest.get_embedding(conn, asset_id, "other-model") is None


# ------------------------------------------------------------------ blocking


def test_candidate_pairs_time_window():
    base = datetime(2025, 6, 1, 12, 0, 0)
    a = _asset(1, path="a.heic", taken_at=base.isoformat())
    b = _asset(2, path="b.jpg", taken_at=(base + timedelta(seconds=2)).isoformat())
    c = _asset(3, path="c.jpg", taken_at=(base + timedelta(minutes=5)).isoformat())
    pairs, _ = engine.candidate_pairs([a, b, c])
    assert (1, 2) in pairs
    assert (1, 3) not in pairs and (2, 3) not in pairs


def test_candidate_pairs_mtime_dates_excluded_from_time_blocking():
    # Takeout extraction mtimes are archive noise — never time-block on them.
    a = _asset(1, path="x1.jpg", taken_src="mtime")
    b = _asset(2, path="x2.jpg", taken_src="mtime")
    pairs, _ = engine.candidate_pairs([a, b])
    assert pairs == set()


def test_candidate_pairs_stem_and_copy_markers():
    a = _asset(1, path="/ic/IMG_1234.HEIC", taken_at=None)
    b = _asset(2, path="/gt/IMG_1234(1).JPG", taken_at=None)
    pairs, _ = engine.candidate_pairs([a, b])
    assert (1, 2) in pairs


def test_candidate_pairs_geo_cell():
    a = _asset(1, path="p1.jpg", gps=(41.38010, 2.17340))
    b = _asset(2, path="p2.jpg", gps=(41.38015, 2.17345),
               taken_at="2025-06-01T12:30:00")
    # different stems, >3s apart — only the geo block can pair them
    a = engine.AssetLite(**{**a.__dict__, "taken_at": "2025-06-01T12:00:00"})
    pairs, _ = engine.candidate_pairs([a, b])
    assert (1, 2) in pairs


def test_oversized_block_skipped():
    crowd = [
        _asset(i, path=f"dup_{i}.jpg", taken_at=None)
        for i in range(engine.MAX_BLOCK + 1)
    ]
    # force one shared stem
    crowd = [
        engine.AssetLite(**{**a.__dict__, "path": "/x/same_stem.jpg"}) for a in crowd
    ]
    crowd = [engine.AssetLite(**{**a.__dict__, "id": i}) for i, a in enumerate(crowd)]
    pairs, warnings = engine.candidate_pairs(crowd)
    assert pairs == set()
    assert warnings and "skipped" in warnings[0]


def test_normalized_stem():
    assert engine.normalized_stem("/a/IMG_1234(1).JPG") == "img_1234"
    assert engine.normalized_stem("/a/IMG_1234-edited.jpg") == "img_1234"
    assert engine.normalized_stem("/a/PXL_20240101-EFFECTS.jpg") == "pxl_20240101"


# --------------------------------------------------------- video pair evidence
#
# Regression coverage for the 2026-07-12 fix: generic camera filename
# counters (IMG_0001.MOV...) recur over 15+ years of device use, and videos
# get no pHash to reject a coincidental stem match later. A bare shared
# block is only trustworthy evidence for a video pair when it's checkable:
# byte-identical, or both sides carry an independently reliable capture
# time that's actually close together.


def _video(id: int, **kwargs) -> engine.AssetLite:
    kwargs.setdefault("media_type", "video")
    kwargs.setdefault("format", "mov")
    kwargs.setdefault("phash_value", None)
    return _asset(id, **kwargs)


def test_pair_evidence_video_byte_identical_is_strong_regardless_of_dates():
    a = _video(1, path="IMG_0580.MOV", bytes=123456,
               taken_at="2011-01-01T00:00:00", taken_src="mtime")
    b = _video(2, path="IMG_0580.MOV", bytes=123456,
               taken_at="2026-06-20T00:00:00", taken_src="mtime")
    assert engine._pair_evidence(a, b) == ("strong", None)


def test_pair_evidence_video_reliable_close_dates_is_candidate():
    a = _video(1, path="IMG_0680.MOV", bytes=100,
               taken_at="2019-04-14T20:38:51", taken_src="exif")
    b = _video(2, path="IMG_0683.MOV", bytes=200,
               taken_at="2019-04-14T21:01:23", taken_src="exif")
    assert engine._pair_evidence(a, b) == ("candidate", None)


def test_pair_evidence_video_reliable_but_far_apart_is_rejected():
    # The actual bug: two real EXIF-dated clips 14 months apart, coincidental
    # filename-counter collision (confirmed live: DJI_0079 SF <-> Cyprus).
    a = _video(1, path="DJI_0079.MOV", bytes=100,
               taken_at="2024-03-18T03:18:59", taken_src="exif")
    b = _video(2, path="DJI_0079.MP4", bytes=200,
               taken_at="2025-05-18T15:55:10", taken_src="exif")
    assert engine._pair_evidence(a, b) is None


def test_pair_evidence_video_unreliable_timestamp_is_rejected():
    # taken_src='mtime' reflects copy/unpack time, not shoot time -- a bare
    # stem match plus an unreliable timestamp on either side is coincidence.
    a = _video(1, path="IMG_0001.MOV", bytes=100, taken_src="exif",
               taken_at="2020-01-01T00:00:00")
    b = _video(2, path="IMG_0001.MOV", bytes=200, taken_src="mtime",
               taken_at="2020-01-01T00:05:00")
    assert engine._pair_evidence(a, b) is None

    c = _video(3, path="IMG_0001.MOV", bytes=100, taken_src="mtime",
               taken_at="2026-06-20T00:00:00")
    d = _video(4, path="IMG_0001.MOV", bytes=200, taken_src="mtime",
               taken_at="2026-06-20T00:03:00")
    assert engine._pair_evidence(c, d) is None


def test_pair_evidence_video_companion_source_counts_as_reliable():
    # DJI SRT-derived time ranks above filename/mtime in dates.py and is
    # real capture time, not a copy-time artifact -- must not be silently
    # excluded from candidate pairing.
    a = _video(1, path="DJI_0100.MP4", bytes=100,
               taken_at="2025-06-10T09:00:00", taken_src="companion")
    b = _video(2, path="DJI_0101.MP4", bytes=200,
               taken_at="2025-06-10T09:04:00", taken_src="companion")
    assert engine._pair_evidence(a, b) == ("candidate", None)


def test_pair_evidence_raw_jpeg_companion_is_not_a_dupe():
    # DJI_0655.DNG + DJI_0655.JPG: same shutter press, two output formats,
    # both always kept -- RAW decodes to a preview visually near-identical
    # to its JPEG twin, so phash alone would call this "strong" and it would
    # get stuck in review forever (member.source == 'originals' guard).
    raw = _asset(1, source="originals", path="/trip/DJI_0655.DNG", format="dng",
                 phash_value=0xAAAA5555AAAA5555)
    jpg = _asset(2, source="originals", path="/trip/DJI_0655.JPG", format="jpg",
                 phash_value=0xAAAA5555AAAA5555)
    assert engine._pair_evidence(raw, jpg) is None
    assert engine._pair_evidence(jpg, raw) is None


def test_pair_evidence_raw_jpeg_different_stem_still_compared_normally():
    # Different capture numbers -- not a companion pair, phash still applies.
    raw = _asset(1, source="originals", path="/trip/DJI_0655.DNG", format="dng",
                 phash_value=0xAAAA5555AAAA5555)
    jpg = _asset(2, source="originals", path="/trip/DJI_0656.JPG", format="jpg",
                 phash_value=0xFFFF0000FFFF0000)
    assert engine._pair_evidence(raw, jpg) is None  # phash distance too large, not a companion-guard result
    # sanity: same stem in a DIFFERENT directory doesn't trip the guard either
    raw2 = _asset(3, source="originals", path="/tripA/DJI_0655.DNG", format="dng",
                  phash_value=0xAAAA5555AAAA5555)
    jpg2 = _asset(4, source="originals", path="/tripB/DJI_0655.JPG", format="jpg",
                  phash_value=0xAAAA5555AAAA5555)
    assert engine._pair_evidence(raw2, jpg2) == ("strong", 0)


# ------------------------------------------------------------------- scoring


def test_winner_score_prefers_icloud_heic_over_google_jpeg():
    icloud = _asset(1, source="icloud", format="heic", bytes=4_000_000,
                    width=4032, height=3024, exif_fields=60)
    google = _asset(2, source="google", format="jpg", bytes=1_200_000,
                    width=2048, height=1536, exif_fields=15)
    assert engine.winner_score(icloud) > engine.winner_score(google)


def test_winner_score_canonical_outranks_equal_icloud():
    existing = _asset(1, source="originals")
    newcomer = _asset(2, source="icloud")
    assert engine.winner_score(existing) > engine.winner_score(newcomer)


# ------------------------------------------------------------------ deciding


def test_decide_auto_merge_on_strong_phash_and_time():
    winner = _asset(1, source="icloud", phash_value=0xFF00FF00FF00FF00)
    loser = _asset(2, source="google", path="IMG_9999.jpg", format="jpg",
                   bytes=900_000, width=2016, height=1512, exif_fields=10,
                   phash_value=0xFF00FF00FF00FF01)  # hamming 1
    assert engine._decide_one([winner, loser]) == "auto"


def test_decide_review_when_phash_weak():
    a = _asset(1, phash_value=0xFF00FF00FF00FF00)
    b = _asset(2, source="google", path="b.jpg",
               phash_value=0x00FF00FF00FF00FF)  # hamming 64
    assert engine._decide_one([a, b]) == "review"


def test_decide_burst_kept_all():
    members = [
        _asset(i, burst_uuid="B-1", path=f"IMG_{i}.HEIC") for i in (1, 2, 3)
    ]
    assert engine._decide_one(members) == "kept_all"


def test_decide_rapid_series_same_dims_kept_all():
    base = datetime(2025, 6, 1, 12, 0, 0)
    members = [
        _asset(i, path=f"IMG_{i}.HEIC",
               taken_at=(base + timedelta(seconds=0.3 * i)).isoformat())
        for i in (1, 2, 3)
    ]
    assert engine._decide_one(members) == "kept_all"


def test_decide_edited_mix_review():
    a = _asset(1)
    b = _asset(2, path="IMG_0001-edited.jpg", edited=True,
               phash_value=0xAAAA5555AAAA5554)
    assert engine._decide_one([a, b]) == "review"


def test_decide_crop_review():
    a = _asset(1, width=4032, height=3024)
    b = _asset(2, source="google", path="b.jpg", width=4032, height=4032,
               phash_value=0xAAAA5555AAAA5554)
    assert engine._decide_one([a, b]) == "review"


def test_decide_displacing_canonical_review():
    # A newcomer that outscores an existing library file → swap → human.
    canonical = _asset(1, source="originals", format="jpg", bytes=800_000,
                       width=1024, height=768, exif_fields=5)
    newcomer = _asset(2, source="icloud", format="heic", bytes=6_000_000,
                      width=8064, height=6048, exif_fields=80,
                      phash_value=0xAAAA5555AAAA5554)
    assert engine._decide_one([canonical, newcomer]) == "review"


# --------------------------------------------------- Stage C (CLIP confirm)


def test_decide_review_stays_review_below_clip_threshold():
    a = _asset(1, phash_value=0xFF00FF00FF00FF00)
    b = _asset(2, source="google", path="b.jpg",
               phash_value=0xFF00FF00FF00FFFF)  # hamming 8 — candidate tier
    assert engine._decide_one([a, b], clip_cos=0.90) == "review"


def test_decide_auto_when_clip_confirms_weak_phash():
    a = _asset(1, phash_value=0xFF00FF00FF00FF00)
    b = _asset(2, source="google", path="b.jpg",
               phash_value=0xFF00FF00FF00FFFF)  # hamming 8 — candidate tier
    assert engine._decide_one([a, b], clip_cos=engine.CLIP_AUTO_THRESHOLD) == "auto"


def test_decide_clip_does_not_override_guards():
    # A perfect CLIP match must not bypass the edited-mix guard — Stage C
    # only substitutes for the missing strong-pHash signal, never the guards.
    a = _asset(1)
    b = _asset(2, path="IMG_0001-edited.jpg", edited=True,
               phash_value=0xAAAA5555AAAA5554)
    assert engine._decide_one([a, b], clip_cos=1.0) == "review"


# ----------------------------------------- commit_cluster_decision (shared)
#
# The one persisted write path for cluster decisions, shared by decide()
# (Stage D) and the manual review tool. The status-transition contract here
# is what `dedup apply` builds on — regressions would silently corrupt the
# promote/quarantine flow, so it's pinned directly.


def _seed_cluster(tmp_path, statuses: dict[int, str]):
    """A manifest with one cluster whose members carry the given statuses."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    conn.execute("INSERT INTO cluster (id, decision) VALUES (1, 'review')")
    for asset_id, status in statuses.items():
        conn.execute(
            "INSERT INTO asset (id, source, path, status, media_type) "
            "VALUES (?, ?, ?, ?, 'image')",
            (asset_id,
             "originals" if status == manifest.CANONICAL else "icloud",
             f"/staging/IMG_{asset_id}.HEIC", status),
        )
        conn.execute(
            "INSERT INTO membership (cluster_id, asset_id) VALUES (1, ?)",
            (asset_id,),
        )
    conn.commit()
    return conn


def test_commit_cluster_decision_auto_advances_clustered_only(tmp_path):
    conn = _seed_cluster(tmp_path, {
        1: manifest.CLUSTERED, 2: manifest.CLUSTERED, 3: manifest.CANONICAL,
    })
    members = [_asset(1), _asset(2), _asset(3, source="originals")]
    engine.commit_cluster_decision(conn, 1, members, "auto", winner_id=3,
                                   confidence=0.9)

    decision, winner, confidence = conn.execute(
        "SELECT decision, winner_asset_id, confidence FROM cluster WHERE id=1"
    ).fetchone()
    assert (decision, winner, confidence) == ("auto", 3, 0.9)

    roles = dict(conn.execute("SELECT asset_id, role FROM membership"))
    assert roles == {1: "loser", 2: "loser", 3: "winner"}

    statuses = dict(conn.execute("SELECT id, status FROM asset"))
    # clustered members advance to decided (what `apply` selects on);
    # the canonical member must NOT move — apply never touches canonicals.
    assert statuses == {
        1: manifest.DECIDED, 2: manifest.DECIDED, 3: manifest.CANONICAL,
    }


def test_commit_cluster_decision_kept_all_touches_no_status(tmp_path):
    conn = _seed_cluster(tmp_path, {1: manifest.CLUSTERED, 2: manifest.CLUSTERED})
    members = [_asset(1), _asset(2)]
    engine.commit_cluster_decision(conn, 1, members, "kept_all", winner_id=1)

    decision, winner = conn.execute(
        "SELECT decision, winner_asset_id FROM cluster WHERE id=1"
    ).fetchone()
    assert (decision, winner) == ("kept_all", 1)

    roles = dict(conn.execute("SELECT asset_id, role FROM membership"))
    assert roles == {1: "winner", 2: "member"}

    # kept_all members stay `clustered` forever — same as machine-made
    # kept_all clusters; advancing them would break cluster() re-entry.
    statuses = set(conn.execute("SELECT status FROM asset"))
    assert statuses == {(manifest.CLUSTERED,)}


def test_commit_cluster_decision_commits_immediately(tmp_path):
    conn = _seed_cluster(tmp_path, {1: manifest.CLUSTERED, 2: manifest.CLUSTERED})
    engine.commit_cluster_decision(conn, 1, [_asset(1), _asset(2)], "auto",
                                   winner_id=1)
    # A second connection must see the decision without conn committing again
    # (the review tool's per-request connections rely on this).
    other = sqlite3.connect(tmp_path / "m.sqlite")
    assert other.execute("SELECT decision FROM cluster WHERE id=1").fetchone() == ("auto",)


def test_load_cluster_members_includes_all_statuses(tmp_path):
    conn = _seed_cluster(tmp_path, {
        1: manifest.CLUSTERED, 2: manifest.CANONICAL,
    })
    members = engine.load_cluster_members(conn, 1)
    assert [m.id for m in members] == [1, 2]
    assert members[1].source == "originals"


# ------------------------------------------------- pixel signals (rescore)


def test_ncc_separates_same_frame_from_distinct(tmp_path):
    from immy.dedup import signals

    same_a, same_b = tmp_path / "a.jpg", tmp_path / "a2.jpg"
    _write_test_jpeg(same_a, seed=5)
    import pyvips
    # a heavy recompress+downscale of the SAME frame — the case pHash can
    # miss but NCC must catch
    pyvips.Image.thumbnail(str(same_a), 300).jpegsave(str(same_b), Q=40)
    other = tmp_path / "b.jpg"
    _write_test_jpeg(other, seed=6)

    g = signals._gray
    assert signals.ncc(g(same_a), g(same_b)) > 0.97
    assert signals.ncc(g(same_a), g(other)) < 0.9


def test_compute_signals_writes_side_table(tmp_path):
    from immy.dedup import signals

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    root = tmp_path / "staging"
    root.mkdir()
    a, b = root / "IMG_1.JPG", root / "IMG_2.JPG"
    _write_test_jpeg(a, seed=9)
    _write_test_jpeg(b, seed=9)  # identical content
    conn.execute("INSERT INTO cluster (id, decision, clip_cos_sim) VALUES (1, 'review', 0.96)")
    for asset_id, path in ((1, a), (2, b)):
        conn.execute(
            "INSERT INTO asset (id, source, path, status, media_type, format,"
            " taken_at, taken_src) VALUES (?, 'icloud', ?, 'clustered', 'image',"
            " 'jpg', ?, 'exif')",
            (asset_id, str(path), f"2025-06-01T12:00:0{asset_id}"),
        )
        conn.execute("INSERT INTO membership (cluster_id, asset_id) VALUES (1, ?)", (asset_id,))
    conn.commit()

    result = signals.compute_signals(conn, tmp_path / "thumbs")
    assert result == {"scored": 1, "no_pixels": 0, "total": 1}
    ncc_value, delta = signals.get_signal(conn, 1)
    assert ncc_value > 0.99         # identical frames
    assert delta == 1.0             # 12:00:01 - 12:00:02
    # idempotent: second run has nothing left to score
    assert signals.compute_signals(conn, tmp_path / "thumbs")["total"] == 0


# ----------------------------------------------------------- end-to-end lite


def test_cluster_and_decide_roundtrip(tmp_path):
    """Two structurally-identical files (one downscaled) + one distinct,
    through register → fingerprint(stub) → cluster → decide."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    root = tmp_path / "staging"
    root.mkdir()
    for name in ("IMG_0001.HEIC", "IMG_0001.JPG", "IMG_0500.JPG"):
        (root / name).write_bytes(b"x" * 1000)
    manifest.register(conn, "icloud", root)

    # Stub fingerprints straight into the manifest (no exiftool in unit tests).
    rows = manifest.pending_fingerprint(conn)
    fingerprints = {
        "IMG_0001.HEIC": {"phash": phash.to_hex(0xFF00FF00FF00FF00),
                          "taken_at": "2025-06-01T12:00:00"},
        "IMG_0001.JPG": {"phash": phash.to_hex(0xFF00FF00FF00FF01),
                         "taken_at": "2025-06-01T12:00:01"},
        "IMG_0500.JPG": {"phash": phash.to_hex(0x0F0F0F0F0F0F0F0F),
                         "taken_at": "2025-06-02T08:00:00"},
    }
    for asset_id, path_text, _ in rows:
        extra = fingerprints[Path(path_text).name]
        manifest.write_fingerprint(conn, asset_id, {
            "media_type": "image", "width": 4032, "height": 3024,
            "taken_src": "exif", "exif_fields": 30, **extra,
        })
    conn.commit()

    result = engine.cluster(conn)
    assert result["clusters_created"] == 1

    counts = engine.decide(conn)
    assert counts["auto"] == 1

    (winner_path,) = conn.execute(
        "SELECT a.path FROM cluster c JOIN asset a ON a.id = c.winner_asset_id"
    ).fetchone()
    assert winner_path.endswith("IMG_0001.HEIC")  # HEIC format bonus wins

    # Idempotency: re-running creates nothing new.
    assert engine.cluster(conn)["clusters_created"] == 0


def test_confirm_clip_unlocks_auto_past_threshold(tmp_path, monkeypatch):
    """A pair too far apart on pHash for Stage B alone (hamming 8, review)
    gets a cached CLIP cosine attached by `confirm_clip`; re-running
    `decide` then upgrades it to `auto` once that cosine clears the bar."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    root = tmp_path / "staging"
    root.mkdir()
    a_path, b_path = root / "IMG_0001.HEIC", root / "IMG_0002.JPG"
    _write_test_jpeg(a_path, seed=1)
    _write_test_jpeg(b_path, seed=2)
    manifest.register(conn, "icloud", root)

    rows = manifest.pending_fingerprint(conn)
    fingerprints = {
        "IMG_0001.HEIC": {"phash": phash.to_hex(0xFF00FF00FF00FF00),
                          "taken_at": "2025-06-01T12:00:00"},
        "IMG_0002.JPG": {"phash": phash.to_hex(0xFF00FF00FF00FFFF),  # hamming 8
                         "taken_at": "2025-06-01T12:00:01"},
    }
    for asset_id, path_text, _ in rows:
        extra = fingerprints[Path(path_text).name]
        manifest.write_fingerprint(conn, asset_id, {
            "media_type": "image", "width": 4032, "height": 3024,
            "taken_src": "exif", "exif_fields": 30, **extra,
        })
    conn.commit()

    assert engine.cluster(conn)["clusters_created"] == 1
    assert engine.decide(conn) == {"auto": 0, "review": 1, "kept_all": 0}

    # Stub the backend: identical vectors → cosine 1.0, well past the bar.
    monkeypatch.setattr(engine.clip_mod, "embed", lambda path, **kw: [1.0, 0.0, 0.0])
    confirm_result = engine.confirm_clip(conn, backend="stub")
    assert confirm_result == {"ok": 1, "failed": 0, "total": 1}

    (cos_sim,) = conn.execute("SELECT clip_cos_sim FROM cluster").fetchone()
    assert cos_sim == pytest.approx(1.0)

    # Cache hit on a second call — no re-embed needed, still just one cluster.
    monkeypatch.setattr(
        engine.clip_mod, "embed",
        lambda path, **kw: (_ for _ in ()).throw(AssertionError("should be cached")),
    )
    assert engine._clip_ready_clusters(conn) == []

    assert engine.decide(conn) == {"auto": 1, "review": 0, "kept_all": 0}

    # Idempotency: re-running creates nothing new.
    assert engine.cluster(conn)["clusters_created"] == 0
    assert engine.decide(conn) == {"auto": 0, "review": 0, "kept_all": 0}
