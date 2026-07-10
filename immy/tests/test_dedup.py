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
    assert engine.decide(conn) == {"auto": 0, "review": 0, "kept_all": 0}
