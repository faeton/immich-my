"""Tests for `immy clip` (Phase Y.3): CLIP embedding + pgvector upsert.

We never actually load the 600 MB MLX model during unit tests — the
`mlx_clip` package is monkey-patched to return a deterministic vector.
What we *do* verify here:

- L2-normalization (Immich stores unit vectors).
- `to_pgvector_literal` round-trips float32 precision.
- `fetch_smart_search_dim` parses `format_type` strings.
- `upsert_smart_search` issues one `INSERT ... ON CONFLICT DO UPDATE`
  with the embedding bound as a text literal (pgvector casts it).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from immy import clip as clip_mod
from immy import pg as pg_mod


# --- clip.embed_image / normalization ------------------------------------


class _FakeMlxClip:
    """Stand-in for `mlx_clip.mlx_clip`. Returns a fixed 4-D vector so the
    test can assert on exact normalized values."""

    def __init__(self, repo_id: str):
        self.repo_id = repo_id

    def image_encoder(self, path: str):
        # Un-normalized; clip.embed_image must normalize before returning.
        return [3.0, 4.0, 0.0, 0.0]


@pytest.fixture(autouse=True)
def _clear_model_cache():
    clip_mod._MODEL_CACHE.clear()
    yield
    clip_mod._MODEL_CACHE.clear()


def test_embed_image_l2_normalizes(monkeypatch, tmp_path: Path):
    import sys
    import types

    fake_mod = types.ModuleType("mlx_clip")
    fake_mod.mlx_clip = _FakeMlxClip  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_clip", fake_mod)

    img = tmp_path / "preview.jpeg"
    img.write_bytes(b"not a real jpeg, but embed_image never opens it here")
    out = clip_mod.embed_image(img)
    assert len(out) == 4
    # sqrt(3^2 + 4^2) = 5 → [0.6, 0.8, 0, 0]
    assert out == pytest.approx([0.6, 0.8, 0.0, 0.0], rel=1e-6)
    assert abs(np.linalg.norm(out) - 1.0) < 1e-6


def test_get_model_raises_when_mlx_clip_missing(monkeypatch):
    import sys

    # Force ImportError from the lazy import inside get_model.
    monkeypatch.setitem(sys.modules, "mlx_clip", None)
    with pytest.raises(clip_mod.ClipUnavailable):
        clip_mod.get_model("ViT-B-32__openai")


def test_get_model_rejects_unknown_immich_name():
    with pytest.raises(clip_mod.ClipUnavailable):
        clip_mod._repo_for("not-a-real-model")


def test_get_model_caches_single_instance(monkeypatch):
    import sys
    import types

    calls = {"n": 0}

    class _Counting(_FakeMlxClip):
        def __init__(self, repo_id: str):
            calls["n"] += 1
            super().__init__(repo_id)

    fake_mod = types.ModuleType("mlx_clip")
    fake_mod.mlx_clip = _Counting  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_clip", fake_mod)

    a = clip_mod.get_model("ViT-B-32__openai")
    b = clip_mod.get_model("ViT-B-32__openai")
    assert a is b
    assert calls["n"] == 1


# --- to_pgvector_literal --------------------------------------------------


def test_to_pgvector_literal_shape():
    s = clip_mod.to_pgvector_literal([0.01, -0.03, 0.5])
    assert s.startswith("[") and s.endswith("]")
    assert s == "[0.01,-0.03,0.5]"


def test_to_pgvector_literal_empty_still_valid():
    assert clip_mod.to_pgvector_literal([]) == "[]"


def test_to_pgvector_literal_preserves_float32_precision():
    # 7 sig figs is the round-trip threshold for float32.
    vec = [0.1234567, -0.7654321]
    out = clip_mod.to_pgvector_literal(vec)
    parsed = [float(x) for x in out.strip("[]").split(",")]
    for a, b in zip(vec, parsed, strict=True):
        assert abs(a - b) < 1e-6


# --- pg.fetch_smart_search_dim -------------------------------------------


def _conn_returning(formatted: str | None):
    """Build a MagicMock connection whose `.execute()` returns one row
    containing `formatted_type` (pgvector's `format_type` output)."""
    conn = MagicMock()
    if formatted is None:
        conn.execute.return_value.fetchone.return_value = None
    else:
        conn.execute.return_value.fetchone.return_value = (formatted,)
    return conn


def test_fetch_smart_search_dim_parses_vector_512():
    conn = _conn_returning("vector(512)")
    assert pg_mod.fetch_smart_search_dim(conn) == 512


def test_fetch_smart_search_dim_parses_vector_768():
    conn = _conn_returning("vector(768)")
    assert pg_mod.fetch_smart_search_dim(conn) == 768


def test_fetch_smart_search_dim_returns_none_for_untyped_vector():
    conn = _conn_returning("vector")
    assert pg_mod.fetch_smart_search_dim(conn) is None


def test_fetch_smart_search_dim_missing_column_raises():
    conn = _conn_returning(None)
    with pytest.raises(LookupError):
        pg_mod.fetch_smart_search_dim(conn)


# --- pg.upsert_smart_search ----------------------------------------------


def test_upsert_smart_search_binds_literal_and_uses_on_conflict():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur

    pg_mod.upsert_smart_search(conn, "asset-uuid", "[0.1,0.2,0.3]")

    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args.args
    assert "INSERT INTO smart_search" in sql
    assert "ON CONFLICT" in sql
    assert "::vector" in sql
    assert params == {"asset_id": "asset-uuid", "embedding": "[0.1,0.2,0.3]"}
