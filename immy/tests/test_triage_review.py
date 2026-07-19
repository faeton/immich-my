"""Triage review server: rollups, take rendering, the verdict write path,
and the frame/video endpoints. Flask test client only — no browser, no
real media; the one "video" served is a stub file on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from immy.dedup import manifest
from immy.triage import review


# ------------------------------------------------------------------ fixture


def _seed(conn, id: int, path: str, *, bytes: int = 10**9,
          taken_at: str | None = None, take_group: int | None = 1,
          suggested: str | None = None, frames: int = 2) -> None:
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, mtime, "
        "  media_type, format, taken_at) VALUES (?, 'originals', ?, "
        "  'canonical', ?, 0, 'video', 'mp4', ?)",
        (id, path, bytes, taken_at),
    )
    conn.execute(
        "INSERT INTO video_signal (asset_id, duration_s, codec, bitrate_kbps, "
        "  take_group, suggested, frames_json) VALUES (?, 60, 'hevc', 50000, ?, ?, ?)",
        (id, take_group, suggested,
         json.dumps([f"{id}/f{i}.jpg" for i in range(frames)])),
    )


@pytest.fixture
def app(tmp_path: Path):
    db = tmp_path / "m.sqlite"
    conn = manifest.open_manifest(db)
    _seed(conn, 1, "/originals/2024-04-namibia/DJI_0001.MP4",
          bytes=8 * 10**9, taken_at="2024-04-01T10:00:00", suggested="compress")
    _seed(conn, 2, "/originals/2024-04-namibia/DJI_0002.MP4",
          taken_at="2024-04-01T10:01:00")
    _seed(conn, 3, "/originals/2024-04-namibia/VID_0003.insv",
          taken_at="2024-04-02T10:00:00", take_group=2)
    _seed(conn, 4, "/originals/2025-06-svalbard-arctic/DJI_0004.MP4",
          take_group=3)
    conn.commit()
    conn.close()

    frames_root = tmp_path / "frames"
    (frames_root / "1").mkdir(parents=True)
    (frames_root / "1" / "f0.jpg").write_bytes(b"\xff\xd8jpeg")

    fs_root = tmp_path / "originals"
    (fs_root / "2024-04-namibia").mkdir(parents=True)
    (fs_root / "2024-04-namibia" / "DJI_0001.MP4").write_bytes(b"mp4bytes")

    flask_app = review.create_app(db, frames_root, "/originals", str(fs_root))
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# -------------------------------------------------------------------- pages


def test_index_lists_trips_biggest_undecided_first(client):
    page = client.get("/").get_data(as_text=True)
    assert page.index("2024-04-namibia") < page.index("2025-06-svalbard-arctic")
    assert "3/4" not in page  # nothing decided yet


def test_trip_page_groups_takes_and_marks_playability(client):
    page = client.get("/trip/2024-04-namibia").get_data(as_text=True)
    # clips 1+2 share take_group 1 → one multi-clip take block
    assert page.count("same scene, pick the best") == 1
    assert "DJI_0001.MP4" in page and "VID_0003.insv" in page
    # the insv clip gets no play button; the mp4s do
    assert page.count("play</button>") == 2
    assert client.get("/trip/nope").status_code == 404


# ------------------------------------------------------------------ verdict


def test_verdict_write_update_and_clear(client, tmp_path):
    res = client.post("/api/verdict",
                      json={"asset_ids": [1, 2], "verdict": "compress",
                            "reason": "take-group bulk"})
    assert res.status_code == 200 and res.get_json()["count"] == 2

    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    rows = dict(conn.execute("SELECT asset_id, verdict FROM triage"))
    assert rows == {1: "compress", 2: "compress"}
    reason, by = conn.execute(
        "SELECT reason, decided_by FROM triage WHERE asset_id=1"
    ).fetchone()
    assert (reason, by) == ("take-group bulk", "human")
    conn.close()

    # re-verdict updates in place; clear deletes the row
    assert client.post("/api/verdict",
                       json={"asset_ids": [1], "verdict": "keep"}).status_code == 200
    assert client.post("/api/verdict",
                       json={"asset_ids": [2], "verdict": "clear"}).status_code == 200
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    assert dict(conn.execute("SELECT asset_id, verdict FROM triage")) == {1: "keep"}
    conn.close()


def test_verdict_rejects_garbage(client):
    assert client.post("/api/verdict", json={"asset_ids": [1], "verdict": "shred"}).status_code == 400
    assert client.post("/api/verdict", json={"asset_ids": [], "verdict": "keep"}).status_code == 400
    assert client.post("/api/verdict", json={"asset_ids": ["x"], "verdict": "keep"}).status_code == 400
    res = client.post("/api/verdict", json={"asset_ids": [999], "verdict": "keep"})
    assert res.status_code == 400 and "never scanned" in res.get_json()["error"]


def test_verdict_locked_after_executor_applied(client, tmp_path):
    client.post("/api/verdict", json={"asset_ids": [4], "verdict": "trash"})
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    conn.execute("UPDATE triage SET applied_at='2026-07-19T00:00:00' WHERE asset_id=4")
    conn.commit()
    conn.close()
    res = client.post("/api/verdict", json={"asset_ids": [4], "verdict": "keep"})
    assert res.status_code == 409
    res = client.post("/api/verdict", json={"asset_ids": [4], "verdict": "clear"})
    assert res.status_code == 409


# ---------------------------------------------------------------- media


def test_frame_serves_cache_and_blocks_traversal(client):
    assert client.get("/frame/1/f0.jpg").status_code == 200
    assert client.get("/frame/1/f1.jpg").status_code == 404      # not cached
    assert client.get("/frame/1/..%2Fsecret.jpg").status_code == 404


def test_video_streams_playable_and_refuses_insv(client):
    res = client.get("/video/1")
    assert res.status_code == 200
    assert res.data == b"mp4bytes"
    assert client.get("/video/3").status_code == 415   # .insv
    assert client.get("/video/2").status_code == 404   # playable but not on disk
    assert client.get("/video/999").status_code == 404
