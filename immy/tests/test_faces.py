"""Phase Y.4 — pure-Python face-module unit tests.

Vision and insightface/onnxruntime are heavyweight and model-gated;
these tests exercise only the logic that doesn't need them: pgvector
literal formatting, dataclass plumbing, and the lazy-import error path.
Wiring into the DB pipeline is covered by test_process.py.
"""

from __future__ import annotations

import numpy as np

from immy import faces as faces_mod


def test_pgvector_literal_formats_512_floats():
    emb = np.arange(512, dtype=np.float32) / 512.0
    literal = faces_mod.to_pgvector_literal(emb)
    assert literal.startswith("[") and literal.endswith("]")
    parts = literal[1:-1].split(",")
    assert len(parts) == 512
    assert float(parts[0]) == 0.0
    assert abs(float(parts[-1]) - 511 / 512) < 1e-6


def test_pgvector_literal_roundtrip_preserves_float32_precision():
    rng = np.random.default_rng(42)
    emb = rng.standard_normal(512).astype(np.float32)
    emb = emb / np.linalg.norm(emb)
    literal = faces_mod.to_pgvector_literal(emb)
    parsed = np.array(
        [float(x) for x in literal[1:-1].split(",")], dtype=np.float32,
    )
    # 7 sig figs is enough for float32 round-trip.
    assert np.max(np.abs(parsed - emb)) < 1e-6


def test_embed_faces_empty_list_short_circuits():
    # No cv2/insightface import should happen for the empty path.
    assert faces_mod.embed_faces(b"", [], "buffalo_l") == []


def test_detected_face_dataclass_defaults():
    f = faces_mod.DetectedFace(x1=1, y1=2, x2=3, y2=4, score=0.9)
    assert f.landmarks is None
    assert f.score == 0.9


def test_use_per_face_inference_only_for_fixed_batch_one_models():
    class Model:
        output_shape = [1, faces_mod.ARCFACE_EMBEDDING_DIM]

    assert faces_mod._use_per_face_inference(Model(), 2) is True
    assert faces_mod._use_per_face_inference(Model(), 1) is False


def test_use_per_face_inference_skips_dynamic_shapes():
    class Model:
        output_shape = [None, faces_mod.ARCFACE_EMBEDDING_DIM]

    assert faces_mod._use_per_face_inference(Model(), 2) is False


# --- pg.replace_asset_faces: never orphan a person's face ------------------


class _FaceTable:
    """In-memory `asset_face` that interprets exactly the statements
    `pg.replace_asset_faces` issues — so the test checks which rows survive,
    not which SQL strings were sent."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.face_search: list[str] = []
        self._result: list[tuple] = []

    # connection API
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # cursor API
    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        aid = (params or {}).get("asset_id")
        if text.startswith("SELECT") and '"personGroupId" IS NOT NULL' in text:
            self._result = [
                (r["x1"], r["y1"], r["x2"], r["y2"], r["w"], r["h"])
                for r in self.rows
                if r["asset"] == aid and r["person"] is not None
            ]
        elif text.startswith("DELETE FROM asset_face"):
            assert "\"sourceType\" = 'machine-learning'" in text
            only_unassigned = '"personGroupId" IS NULL' in text
            self.rows = [
                r for r in self.rows
                if not (r["asset"] == aid and r["source"] == "machine-learning"
                        and (r["person"] is None or not only_unassigned))
            ]
        elif text.startswith("INSERT INTO asset_face"):
            self.rows.append({
                "id": params["id"], "asset": aid, "person": None,
                "source": "machine-learning",
                "x1": params["x1"], "y1": params["y1"],
                "x2": params["x2"], "y2": params["y2"],
                "w": params["image_width"], "h": params["image_height"],
            })
        elif text.startswith("INSERT INTO face_search"):
            self.face_search.append(params["face_id"])
        else:  # pragma: no cover - a statement the fake doesn't model
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return self._result


def _row(fid, person, x1, y1, x2, y2, *, w=1000, h=1000,
         source="machine-learning", asset="a1"):
    return {"id": fid, "asset": asset, "person": person, "source": source,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "w": w, "h": h}


def _new(fid, x1, y1, x2, y2):
    return {"id": fid, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "embedding": "[0]"}


def test_replace_asset_faces_keeps_person_assigned_faces():
    from immy import pg as pg_mod

    table = _FaceTable([
        _row("named", "person-1", 100, 100, 200, 200),
        _row("unassigned", None, 500, 500, 600, 600),
        _row("exif", None, 700, 700, 800, 800, source="exif"),
        _row("other-asset", None, 0, 0, 10, 10, asset="a2"),
    ])
    pg_mod.replace_asset_faces(table, "a1", 1000, 1000, [
        _new("fresh", 300, 300, 400, 400),
    ])
    ids = {r["id"] for r in table.rows}
    assert "named" in ids                 # the person keeps their face
    assert "unassigned" not in ids        # stale unassigned ML face replaced
    assert {"exif", "other-asset", "fresh"} <= ids


def test_replace_asset_faces_skips_new_box_overlapping_assigned_face():
    """A re-detection of the same face (IoU >= 0.5, compared in normalized
    coords so a different detection resolution still matches) is dropped:
    the person keeps exactly one face there. A non-overlapping new box is
    inserted with its embedding."""
    from immy import pg as pg_mod

    table = _FaceTable([
        # stored at 2000x2000 → normalized (0.1,0.1)-(0.2,0.2)
        _row("named", "person-1", 200, 200, 400, 400, w=2000, h=2000),
    ])
    written = pg_mod.replace_asset_faces(table, "a1", 1000, 1000, [
        _new("dup", 102, 98, 205, 201),       # same face, new run
        _new("elsewhere", 600, 600, 700, 700),
    ])
    ids = [r["id"] for r in table.rows]
    assert ids.count("named") == 1
    assert "dup" not in ids
    assert "elsewhere" in ids
    assert table.face_search == ["elsewhere"]
    assert written == 1


def test_replace_asset_faces_low_overlap_box_is_inserted():
    from immy import pg as pg_mod

    table = _FaceTable([_row("named", "person-1", 100, 100, 200, 200)])
    # IoU = 2500 / 17500 ≈ 0.14 — a neighbouring face, not the same one.
    pg_mod.replace_asset_faces(table, "a1", 1000, 1000, [
        _new("neighbour", 150, 150, 250, 250),
    ])
    assert {r["id"] for r in table.rows} == {"named", "neighbour"}


def test_replace_asset_faces_kept_face_without_dimensions_uses_detection_size():
    """A kept person face stored with imageWidth/Height = 0 is placed in the
    current detection's frame (same asset) — so an identical re-detection is
    still recognised as the same face, not inserted beside it."""
    from immy import pg as pg_mod

    table = _FaceTable([_row("named", "person-1", 100, 100, 200, 200, w=0, h=0)])
    written = pg_mod.replace_asset_faces(table, "a1", 1000, 1000, [
        _new("dup", 100, 100, 200, 200),
        _new("elsewhere", 600, 600, 700, 700),
    ])
    ids = {r["id"] for r in table.rows}
    assert "dup" not in ids
    assert {"named", "elsewhere"} <= ids
    assert written == 1


def test_replace_asset_faces_no_usable_dimensions_skips_new_boxes_near_kept():
    """Neither the kept face nor the detection has a usable size: overlap
    can't be measured, so err on not duplicating the person's face."""
    from immy import pg as pg_mod

    table = _FaceTable([_row("named", "person-1", 100, 100, 200, 200, w=0, h=0)])
    written = pg_mod.replace_asset_faces(table, "a1", 0, 0, [
        _new("dup", 100, 100, 200, 200),
    ])
    assert {r["id"] for r in table.rows} == {"named"}
    assert written == 0
