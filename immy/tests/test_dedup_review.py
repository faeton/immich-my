"""Route-level tests for the manual dedup review tool (`dedup review-server`).

Engine-level correctness of the decision write lives in test_dedup.py
(commit_cluster_decision tests) — these cover the HTTP layer: queue order,
validation, the stale-tab 409, and the originals-winner guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("flask")

from immy.dedup import manifest, review  # noqa: E402


def _seed(tmp_path: Path):
    """Two review clusters (high + low clip score) and one already-auto one.

    Cluster 1 (cos 0.99): two icloud/google images — the plain case.
    Cluster 2 (cos 0.95): google image + an originals member — guard case.
    Cluster 3: already decided 'auto' — must never be re-writable.
    """
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    rows = [
        # id, source, path, status, cluster, clip
        (1, "icloud", "/staging/icloud/IMG_1.HEIC", manifest.CLUSTERED),
        (2, "google", "/staging/google/IMG_1.JPG", manifest.CLUSTERED),
        (3, "google", "/staging/google/IMG_2.JPG", manifest.CLUSTERED),
        (4, "originals", "/originals/2024/06/IMG_2.HEIC", manifest.CANONICAL),
        (5, "icloud", "/staging/icloud/IMG_3.HEIC", manifest.DECIDED),
        (6, "google", "/staging/google/IMG_3.JPG", manifest.DECIDED),
    ]
    for asset_id, source, path, status in rows:
        conn.execute(
            "INSERT INTO asset (id, source, path, status, media_type, format,"
            " width, height, bytes, exif_fields)"
            " VALUES (?, ?, ?, ?, 'image', 'jpg', 4032, 3024, 1000000, 30)",
            (asset_id, source, path, status),
        )
    clusters = [(1, "review", 0.99), (2, "review", 0.95), (3, "auto", 1.0)]
    for cid, decision, cos in clusters:
        conn.execute(
            "INSERT INTO cluster (id, decision, clip_cos_sim) VALUES (?, ?, ?)",
            (cid, decision, cos),
        )
    for cid, aid in [(1, 1), (1, 2), (2, 3), (2, 4), (3, 5), (3, 6)]:
        conn.execute(
            "INSERT INTO membership (cluster_id, asset_id) VALUES (?, ?)",
            (cid, aid),
        )
    conn.commit()
    conn.close()
    return tmp_path / "m.sqlite"


@pytest.fixture
def client(tmp_path):
    db_path = _seed(tmp_path)
    app = review.create_app(db_path, tmp_path / "thumbs")
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, db_path


def _decision(db_path, cluster_id):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT decision, winner_asset_id FROM cluster WHERE id=?",
            (cluster_id,),
        ).fetchone()
    finally:
        conn.close()


def test_index_serves_highest_clip_cluster_first(client):
    c, _ = client
    page = c.get("/").get_data(as_text=True)
    assert "cluster 1" in page
    assert "0.990000" in page


def test_merge_writes_auto_and_advances_queue(client):
    c, db_path = client
    res = c.post("/api/decide/1", json={"action": "merge", "winner_asset_id": 1})
    assert res.status_code == 200
    assert _decision(db_path, 1) == ("auto", 1)
    # decided cluster drops out of the queue; next page is cluster 2
    assert "cluster 2" in c.get("/").get_data(as_text=True)


def test_merge_rejects_non_member_winner(client):
    c, db_path = client
    res = c.post("/api/decide/1", json={"action": "merge", "winner_asset_id": 99})
    assert res.status_code == 400
    assert _decision(db_path, 1) == ("review", None)


def test_merge_guard_locks_winner_to_originals_member(client):
    c, db_path = client
    # picking the google member over the originals one must be refused
    res = c.post("/api/decide/2", json={"action": "merge", "winner_asset_id": 3})
    assert res.status_code == 400
    assert b"originals" in res.data
    assert _decision(db_path, 2) == ("review", None)
    # the originals member itself is a valid winner
    res = c.post("/api/decide/2", json={"action": "merge", "winner_asset_id": 4})
    assert res.status_code == 200
    assert _decision(db_path, 2) == ("auto", 4)


def test_keep_all_writes_kept_all(client):
    c, db_path = client
    res = c.post("/api/decide/1", json={"action": "keep_all"})
    assert res.status_code == 200
    decision, winner = _decision(db_path, 1)
    assert decision == "kept_all" and winner is not None


def test_double_submit_is_409_not_overwrite(client):
    c, db_path = client
    assert c.post("/api/decide/1", json={"action": "keep_all"}).status_code == 200
    res = c.post("/api/decide/1", json={"action": "merge", "winner_asset_id": 1})
    assert res.status_code == 409
    assert _decision(db_path, 1)[0] == "kept_all"


def test_already_auto_cluster_is_409(client):
    c, db_path = client
    res = c.post("/api/decide/3", json={"action": "keep_all"})
    assert res.status_code == 409
    assert _decision(db_path, 3)[0] == "auto"


def test_skip_advances_without_writing(client):
    c, db_path = client
    assert c.post("/api/skip/1").status_code == 200
    assert "cluster 2" in c.get("/").get_data(as_text=True)
    assert _decision(db_path, 1) == ("review", None)


def test_originals_cluster_page_shows_lock(client):
    c, _ = client
    page = c.get("/cluster/2").get_data(as_text=True)
    assert "winner is locked" in page


def test_unknown_action_and_cluster(client):
    c, _ = client
    assert c.post("/api/decide/1", json={"action": "explode"}).status_code == 400
    assert c.post("/api/decide/999", json={"action": "keep_all"}).status_code == 404


def test_recommended_keeper_is_preselected_in_html(client):
    """The winner_score-best member must render already selected — Enter
    with zero clicks is the primary review gesture. (Regression: the first
    build only applied the highlight on click, never on initial paint.)"""
    c, _ = client
    page = c.get("/").get_data(as_text=True)
    assert 'class="card selected"' in page
    assert "recommended keeper" in page


def test_index_prefetches_upcoming_cluster_thumbs(client):
    c, _ = client
    page = c.get("/").get_data(as_text=True)
    # cluster 2's members (assets 3, 4) are the next in queue
    assert "const PREFETCH = [3, 4];" in page


def test_batch_page_lists_pending_clusters_with_winners(client):
    c, _ = client
    page = c.get("/batch").get_data(as_text=True)
    assert 'data-cid="1"' in page and 'data-cid="2"' in page
    # cluster 2's winner is pre-locked to the originals member (asset 4)
    assert 'data-cid="2" data-winner="4"' in page


def test_batch_merges_checked_and_skips_unchecked(client):
    c, db_path = client
    res = c.post("/api/decide-batch", json={
        "decisions": [{"cluster_id": 1, "winner_asset_id": 1}],
        "skip": [2],
    })
    assert res.status_code == 200
    out = res.get_json()
    assert out["merged"] == 1 and out["failed"] == []
    assert _decision(db_path, 1) == ("auto", 1)
    assert _decision(db_path, 2) == ("review", None)  # skip writes nothing
    # both are now out of the queue: 1 decided, 2 session-skipped
    assert "No image review clusters left" in c.get("/batch").get_data(as_text=True)


def test_threshold_sweep_merges_only_at_or_above_cos(client):
    c, db_path = client
    # dry run: only cluster 1 (0.99) clears 0.98; nothing is written
    pre = c.post("/api/decide-threshold", json={"min_cos": 0.98, "dry_run": True})
    assert pre.get_json() == {"count": 1}
    assert _decision(db_path, 1) == ("review", None)
    # real sweep merges cluster 1 with its recommended winner (icloud asset 1
    # outscores google asset 2); cluster 2 (0.95) is untouched
    out = c.post("/api/decide-threshold", json={"min_cos": 0.98}).get_json()
    assert out["merged"] == 1 and out["failed"] == []
    assert _decision(db_path, 1) == ("auto", 1)
    assert _decision(db_path, 2) == ("review", None)


def test_threshold_sweep_uses_originals_winner_and_skips_hesitations(client):
    c, db_path = client
    # a skipped cluster is an explicit hesitation — the sweep must not take it
    c.post("/api/skip/1")
    out = c.post("/api/decide-threshold", json={"min_cos": 0.95}).get_json()
    assert out["merged"] == 1
    assert _decision(db_path, 1) == ("review", None)   # skipped, untouched
    assert _decision(db_path, 2) == ("auto", 4)        # originals member wins


def test_threshold_sweep_refuses_low_bar(client):
    c, _ = client
    assert c.post("/api/decide-threshold", json={"min_cos": 0.5}).status_code == 400
    assert c.post("/api/decide-threshold", json={}).status_code == 400


def test_batch_enforces_originals_guard_per_cluster(client):
    c, db_path = client
    res = c.post("/api/decide-batch", json={"decisions": [
        {"cluster_id": 1, "winner_asset_id": 1},
        {"cluster_id": 2, "winner_asset_id": 3},   # non-originals winner: refused
    ]})
    out = res.get_json()
    assert out["merged"] == 1
    assert len(out["failed"]) == 1 and out["failed"][0]["cluster_id"] == 2
    assert _decision(db_path, 1) == ("auto", 1)
    assert _decision(db_path, 2) == ("review", None)
