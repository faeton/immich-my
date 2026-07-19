"""Footage triage: manifest v3, take-grouping, suggestions, scan, report.

All ffprobe/ffmpeg/CLIP/Immich seams are faked — these tests exercise the
grouping math, the suggestion rules, and the manifest plumbing, never the
network or a real video file.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from immy.dedup import manifest
from immy.triage import engine


# ------------------------------------------------------------------ helpers


def _seed_asset(
    conn,
    id: int,
    path: str,
    *,
    bytes: int = 1_000_000_000,
    mtime: float = 1_000_000.0,
    taken_at: str | None = None,
    media_type: str | None = "video",
    format: str = "mp4",
) -> None:
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, mtime, "
        "  media_type, format, taken_at) VALUES (?, 'originals', ?, "
        "  'canonical', ?, ?, ?, ?, ?)",
        (id, path, bytes, mtime, media_type, format, taken_at),
    )


def _clip(id, trip="2024-03-antarctica", epoch=0.0, vec=None, **kw):
    defaults = dict(
        path=f"/originals/{trip}/DJI_{id:04d}.MP4", bytes=10**9, mtime=epoch,
        taken_at=None,
    )
    defaults.update(kw)
    c = engine.Clip(id=id, trip=trip, **defaults)
    c.vec = vec
    return c


# ------------------------------------------------------------------ schema


def test_manifest_v3_created_fresh(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"triage", "video_signal"} <= tables
    assert manifest.get_meta(conn, "schema_version") == "3"


def test_manifest_v2_migrates_to_v3(tmp_path: Path):
    """A live v2 manifest (n5's real shape) gains the triage tables on open."""
    db = tmp_path / "m.sqlite"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE asset (id INTEGER PRIMARY KEY, source TEXT, "
                "path TEXT UNIQUE, status TEXT, taken_at TEXT, live_cid TEXT)")
    raw.execute("CREATE TABLE cluster (id INTEGER PRIMARY KEY, "
                "winner_asset_id INTEGER, confidence REAL, "
                "decision TEXT DEFAULT 'pending', clip_cos_sim REAL)")
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    raw.commit()
    raw.close()

    conn = manifest.open_manifest(db)
    assert manifest.get_meta(conn, "schema_version") == "3"
    conn.execute(
        "INSERT INTO asset (id, source, path, status) "
        "VALUES (1, 'originals', '/originals/t/a.mp4', 'canonical')"
    )
    conn.execute(
        "INSERT INTO triage (asset_id, verdict, decided_by, decided_at) "
        "VALUES (1, 'keep', 'human', '2026-07-18T00:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO triage (asset_id, verdict, decided_by, decided_at) "
            "VALUES (2, 'shred', 'human', '2026-07-18T00:00:00')"
        )


def test_manifest_v4_refuses(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    manifest.set_meta(conn, "schema_version", "99")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        manifest.open_manifest(tmp_path / "m.sqlite")


# ------------------------------------------------------------------ paths


def test_trip_of_dated_and_trip_dirs():
    assert engine.trip_of("/originals/2024-03-antarctica/a.mp4", "/originals") \
        == "2024-03-antarctica"
    assert engine.trip_of("/originals/2024/06/a.mp4", "/originals") is None
    assert engine.trip_of("/originals/_derivatives/x.mp4", "/originals") is None
    assert engine.trip_of("/elsewhere/trip/a.mp4", "/originals") is None


def test_map_path_host_translation():
    assert engine.map_path(
        "/originals/t/a.mp4", "/originals", "/mnt/tank/immich/originals"
    ) == Path("/mnt/tank/immich/originals/t/a.mp4")
    assert engine.map_path("/originals/t/a.mp4", "/originals", "/originals") \
        == Path("/originals/t/a.mp4")


# ------------------------------------------------------------------ takes


def test_take_groups_split_on_time_gap():
    a = _clip(1, epoch=0.0)
    b = _clip(2, epoch=60.0)      # within gap → same take
    c = _clip(3, epoch=60.0 + 121.0)  # past gap → new take
    engine.assign_take_groups([a, b, c])
    assert a.take_group == b.take_group != c.take_group


def test_take_groups_split_on_cosine():
    same, diff = [1.0, 0.0], [0.0, 1.0]
    a = _clip(1, epoch=0.0, vec=same)
    b = _clip(2, epoch=10.0, vec=same)     # cos=1 → same take
    c = _clip(3, epoch=20.0, vec=diff)     # cos=0 vs centroid → new take
    engine.assign_take_groups([a, b, c])
    assert a.take_group == b.take_group != c.take_group


def test_take_groups_missing_vec_falls_back_to_time():
    a = _clip(1, epoch=0.0, vec=[1.0, 0.0])
    b = _clip(2, epoch=10.0, vec=None)     # no vector — time alone groups it
    engine.assign_take_groups([a, b])
    assert a.take_group == b.take_group


def test_proxies_out_of_scope(tmp_path: Path):
    """.lrv/.lrf are derivable camera proxies excluded from the vv mirror —
    they must never reach the human review queue, and rows a v1 scan wrote
    for them are purged on the next scan."""
    assert engine.is_proxy("/originals/t/LRV_001.lrv")
    assert engine.is_proxy("/originals/t/DJI_0001.LRF")
    # Insta360 stitched preview: proxy by PREFIX, extension is .insv
    assert engine.is_proxy("/originals/t/LRV_20240211_125134_11_053.insv")
    assert not engine.is_proxy("/originals/t/VID_20240211_125134_00_053.insv")

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _seed_asset(conn, 1, "/originals/2024-04-namibia/VID_001.insv", format="insv")
    _seed_asset(conn, 2, "/originals/2024-04-namibia/LRV_001.lrv", format="lrv")
    _seed_asset(conn, 3, "/originals/2024-04-namibia/LRV_20240401_120000_11_007.insv",
                format="insv")
    conn.executemany(
        "INSERT INTO video_signal (asset_id, duration_s) VALUES (?, 60)",
        [(2,), (3,)],
    )  # stale v1/v2 rows for the proxies
    conn.commit()

    assert [c.id for c in engine.load_trip_videos(conn, "/originals")] == [1]
    engine.scan(
        conn, root="/originals", frames_root=tmp_path / "f",
        backend="immich-ml", endpoint="http://ml", model_name="m",
        probe_fn=lambda p: _FakeInfo(10.0),
        extract_fn=_fake_extract, embed_fn=lambda *a, **k: [1.0, 0.0],
        immich_lookup=None,
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM video_signal WHERE asset_id IN (2, 3)"
    ).fetchone()[0] == 0


def test_take_groups_never_cross_trips():
    a = _clip(1, trip="2024-03-antarctica", epoch=0.0)
    b = _clip(2, trip="2025-06-svalbard-arctic", epoch=10.0)
    engine.assign_take_groups([a, b])
    assert a.take_group != b.take_group


# ------------------------------------------------------------------ suggest


def test_suggest_favorite_wins_over_compress():
    c = _clip(1)
    c.duration_s, c.bitrate_kbps, c.favorite = 600.0, 100_000.0, 1
    assert engine.suggest(c, take_size=5) == ("keep", "immich favorite")


def test_suggest_ignores_albums():
    """immy auto-albums cover every trip clip — album membership must not
    suggest keep (it once blanket-kept 1.86 TB)."""
    c = _clip(1)
    c.duration_s, c.bitrate_kbps, c.album_count = 600.0, 100_000.0, 3
    suggested, _ = engine.suggest(c, take_size=1)
    assert suggested == "compress"


def test_suggest_compress_candidate():
    c = _clip(1)
    c.duration_s, c.bitrate_kbps = 300.0, 80_000.0
    suggested, reason = engine.suggest(c, take_size=1)
    assert suggested == "compress"
    assert "300s" in reason and "80 Mbps" in reason


def test_suggest_review_take_and_none():
    c = _clip(1)
    c.duration_s, c.bitrate_kbps = 30.0, 40_000.0
    assert engine.suggest(c, take_size=3) == ("review-take", "3-clip take group")
    assert engine.suggest(c, take_size=2) == (None, None)


# ------------------------------------------------------------------ pooling


def test_pool_frames_mean_and_norm():
    pooled = engine.pool_frames([[2.0, 0.0], [0.0, 2.0]])
    assert pooled == pytest.approx([0.7071, 0.7071], abs=1e-3)


# ------------------------------------------------------------------ scan


class _FakeInfo:
    def __init__(self, duration_s, codec="h264"):
        self.duration_s = duration_s
        self.video_codec = codec


def _fake_extract(src: Path, dst: Path, seek: float) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"jpeg")


def test_scan_end_to_end_and_resume(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _seed_asset(conn, 1, "/originals/2024-03-antarctica/DJI_0001.MP4",
                bytes=8 * 10**9, taken_at="2024-03-01T10:00:00")
    _seed_asset(conn, 2, "/originals/2024-03-antarctica/DJI_0002.MP4",
                taken_at="2024-03-01T10:00:30")
    _seed_asset(conn, 3, "/originals/2024/06/IMG_1.MOV")   # dated → ignored
    _seed_asset(conn, 4, "/originals/2024-03-antarctica/pic.jpg",
                media_type="image", format="jpg")           # image → ignored
    conn.commit()

    embeds: list[int] = []

    def fake_embed(path, *, model_name, backend, endpoint):
        embeds.append(1)
        return [1.0, 0.0]

    kwargs = dict(
        root="/originals", frames_root=tmp_path / "frames",
        backend="immich-ml", endpoint="http://ml", model_name="test-model",
        probe_fn=lambda p: _FakeInfo(100.0),
        extract_fn=_fake_extract, embed_fn=fake_embed,
        immich_lookup=lambda paths: {
            "/originals/2024-03-antarctica/DJI_0001.MP4": (True, 2),
        },
    )
    result = engine.scan(conn, **kwargs)
    assert result["eligible"] == 2
    assert result["scanned_now"] == 2 and result["failed"] == 0
    assert result["immich_flagged"] == 1
    assert len(embeds) == 2 * engine.FRAMES_PER_CLIP

    rows = {
        r[0]: r[1:] for r in conn.execute(
            "SELECT asset_id, duration_s, bitrate_kbps, favorite, "
            "  album_count, take_group, suggested, frames_json FROM video_signal"
        )
    }
    assert set(rows) == {1, 2}
    dur, kbps, fav, albums, tg1, suggested, frames = rows[1]
    assert dur == 100.0
    assert kbps == pytest.approx(8 * 10**9 * 8 / 100.0 / 1000.0)
    assert (fav, albums) == (1, 2)
    assert suggested == "keep"
    assert len(json.loads(frames)) == engine.FRAMES_PER_CLIP
    assert rows[2][4] == tg1                      # 30 s apart → same take
    # pooled vector cached under the scan's model
    assert manifest.get_embedding(conn, 1, "test-model") is not None

    # resume: nothing new to scan, cached signals hydrate, no re-embeds
    embeds.clear()
    result2 = engine.scan(conn, **kwargs)
    assert result2["scanned_now"] == 0 and result2["total_scanned"] == 2
    assert embeds == []


def test_scan_probe_failure_skips_and_retries(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _seed_asset(conn, 1, "/originals/2024-03-antarctica/DJI_0001.MP4")
    conn.commit()

    def broken_probe(path):
        raise engine.VideoProbeError("boom")

    result = engine.scan(
        conn, root="/originals", frames_root=tmp_path / "frames",
        backend="immich-ml", endpoint=None, model_name="test-model",
        probe_fn=broken_probe, extract_fn=_fake_extract,
        embed_fn=lambda *a, **k: [1.0], immich_lookup=None,
    )
    assert result["failed"] == 1
    # no row → the next run retries instead of caching the failure
    assert conn.execute("SELECT COUNT(*) FROM video_signal").fetchone()[0] == 0


# ------------------------------------------------------------------ report


def test_report_aggregates_and_sorts(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    # big trip: 3-clip take (review-take sized) + one compress suggestion
    for i, tg in ((1, 10), (2, 10), (3, 10)):
        _seed_asset(conn, i, f"/originals/2025-12-les-arcs/GX_{i}.MP4",
                    bytes=4 * 10**9)
        conn.execute(
            "INSERT INTO video_signal (asset_id, take_group, suggested, favorite) "
            "VALUES (?, ?, NULL, 0)", (i, tg),
        )
    _seed_asset(conn, 4, "/originals/2025-12-les-arcs/GX_4.MP4", bytes=2 * 10**9)
    conn.execute(
        "INSERT INTO video_signal (asset_id, take_group, suggested, favorite) "
        "VALUES (4, 11, 'compress', 1)"
    )
    # small trip
    _seed_asset(conn, 5, "/originals/2024-04-namibia/DJI_5.MP4", bytes=10**9)
    conn.execute(
        "INSERT INTO video_signal (asset_id, take_group) VALUES (5, 20)"
    )
    conn.commit()

    data = engine.report(conn, root="/originals")
    assert list(data["trips"]) == ["2025-12-les-arcs", "2024-04-namibia"]
    arcs = data["trips"]["2025-12-les-arcs"]
    assert arcs["clips"] == 4
    assert arcs["bytes"] == 14 * 10**9
    assert arcs["take_bytes"] == 12 * 10**9      # only the ≥3-clip take
    assert arcs["compress_bytes"] == 2 * 10**9
    assert arcs["favorites"] == 1
    assert data["totals"]["clips"] == 5
