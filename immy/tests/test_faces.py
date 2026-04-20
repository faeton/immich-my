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
