"""360 viewer: recording grouping, stream-source choice, page/stream routes."""

from __future__ import annotations

from pathlib import Path

import pytest

from immy import pano
from immy.dedup import manifest


def _seed(conn, id: int, path: str, *, bytes: int = 10**9) -> None:
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, media_type, format)"
        " VALUES (?, 'originals', ?, 'canonical', ?, 'video', 'insv')",
        (id, path, bytes),
    )


@pytest.fixture
def conn(tmp_path: Path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    trip = "/originals/2024-02-peru-bolivia"
    _seed(conn, 1, f"{trip}/VID_20240211_125134_00_053.insv", bytes=2 * 10**9)
    _seed(conn, 2, f"{trip}/VID_20240211_125134_10_053.insv", bytes=2 * 10**9)
    _seed(conn, 3, f"{trip}/LRV_20240211_125134_11_053.insv", bytes=5 * 10**8)
    _seed(conn, 4, f"{trip}/VID_20240211_125134_00_053.mp4", bytes=4 * 10**9)
    _seed(conn, 5, f"{trip}/VID_20240301_000000_00_060.insv")   # master only
    _seed(conn, 6, f"{trip}/GX010001.MP4")                      # flat gopro — no group
    _seed(conn, 7, "/originals/2024/02/VID_20240401_000000_00_001.insv")  # dated tree
    # X4/X5-era naming: single-file master + stitched preview as .lrv
    _seed(conn, 8, "/originals/2026-02-mau-whales/VID_20260226_070904_00_006.insv")
    _seed(conn, 9, "/originals/2026-02-mau-whales/LRV_20260226_070904_01_006.lrv")
    conn.execute("INSERT INTO video_signal (asset_id, duration_s) VALUES (1, 95)")
    conn.commit()
    return conn


def test_load_recordings_groups_and_picks_stream(conn):
    recs = pano.load_recordings(conn, "/originals")
    assert len(recs) == 3                       # dated tree + flat mp4 excluded
    x4 = next(r for r in recs if r.key == "20260226_070904_006")
    assert (x4.masters, x4.lrv_id) == ([8], 9)  # .lrv preview streams too
    full = next(r for r in recs if r.key == "20240211_125134_053")
    assert sorted(full.masters) == [1, 2]
    assert full.master_bytes == 4 * 10**9
    assert (full.lrv_id, full.export_id) == (3, 4)
    assert full.stream_id == 3                  # LRV preferred (surely equirect)
    assert full.duration_s == 95
    bare = next(r for r in recs if r.key == "20240301_000000_060")
    assert bare.stream_id is None               # nothing watchable to stream


def test_pages_and_stream_route(conn, tmp_path):
    fs = tmp_path / "originals" / "2024-02-peru-bolivia"
    fs.mkdir(parents=True)
    (fs / "LRV_20240211_125134_11_053.insv").write_bytes(b"lrvdata")

    app = pano.create_app(
        tmp_path / "m.sqlite", tmp_path / "posters",
        "/originals", str(tmp_path / "originals"),
    )
    app.config["TESTING"] = True
    client = app.test_client()

    assert "2024-02-peru-bolivia" in client.get("/").get_data(as_text=True)
    page = client.get("/trip/2024-02-peru-bolivia").get_data(as_text=True)
    assert "full-res export" in page and "camera preview" in page
    assert client.get("/trip/none").status_code == 404

    res = client.get("/stream/3")
    assert res.status_code == 200 and res.data == b"lrvdata"
    assert res.mimetype == "video/mp4"
    assert client.get("/stream/999").status_code == 404
