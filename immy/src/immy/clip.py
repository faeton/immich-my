"""Phase Y.3 — CLIP visual embeddings on Apple Silicon via mlx-clip.

Computes a 512-dim (for the default `ViT-B-32__openai`) normalized float32
vector from the *preview* JPEG staged in Y.2. The vector is written as a
pgvector string literal (e.g. `"[0.01,-0.03,...]"`) into
`smart_search(assetId, embedding)`; pgvector handles the cast.

Why this module stays thin:
- The model is heavy (~600 MB) and slow to load; the wrapper caches a
  single instance per (model, process) via `get_model`.
- Inference itself is the only call site that depends on `mlx_clip`. We
  lazy-import so `import immy.clip` works on machines without MLX — only
  `embed_image` fails, and only when actually used.
- Normalization happens here (L2) — Immich stores unit vectors and the
  HNSW index uses cosine distance.

See docs/IMMICH-INGEST.md §4.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_MODEL = "ViT-B-32__openai"

# Immich model name → mlx-community repo id. Mirrors the accelerator's
# MODEL_MAP for the models we actually support on Mac. Unknown names fall
# through to the default — keep this narrow; changing Immich's CLIP model
# is a deliberate migration, not an auto-handle.
MODEL_REPO = {
    "ViT-B-32__openai": "mlx-community/clip-vit-base-patch32",
    "ViT-B-16__openai": "mlx-community/clip-vit-base-patch16",
    "ViT-L-14__openai": "mlx-community/clip-vit-large-patch14",
    "ViT-B-32__laion2b-s34b-b79k": "mlx-community/clip-vit-base-patch32-laion2b",
    "ViT-B-32__laion2b_s34b_b79k": "mlx-community/clip-vit-base-patch32-laion2b",
}


_MODEL_CACHE: dict[str, Any] = {}


class ClipUnavailable(RuntimeError):
    """Raised when mlx-clip isn't installed or the requested model can't be
    mapped to an MLX repo. Caller decides whether to skip the asset or abort.
    """


def _repo_for(model_name: str) -> str:
    repo = MODEL_REPO.get(model_name)
    if repo is None:
        raise ClipUnavailable(
            f"model {model_name!r} has no MLX mapping; "
            f"supported: {sorted(MODEL_REPO)}"
        )
    return repo


def get_model(model_name: str = DEFAULT_MODEL) -> Any:
    """Lazy-load and cache the mlx-clip model by Immich name.

    First call per process pays the load cost; subsequent calls return the
    cached instance. Raises `ClipUnavailable` if `mlx_clip` is not installed.
    """
    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached
    try:
        from mlx_clip import mlx_clip as _mlx_clip  # type: ignore
    except ImportError as e:
        raise ClipUnavailable(
            "mlx-clip is not installed; `uv add mlx-clip` (Apple Silicon only)"
        ) from e
    model = _mlx_clip(_repo_for(model_name))
    _MODEL_CACHE[model_name] = model
    return model


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return v.astype(np.float32, copy=False)
    return (v / n).astype(np.float32, copy=False)


def embed_image(image_path: Path, model_name: str = DEFAULT_MODEL) -> list[float]:
    """Return an L2-normalized CLIP image embedding as a plain list.

    Feed the preview file (1440 px JPEG from Y.2), not the original — the
    model is resized to 224 internally, and Immich itself encodes the
    preview, so we match its convention for cross-platform search.
    """
    model = get_model(model_name)
    raw = model.image_encoder(str(image_path))
    arr = np.asarray(raw, dtype=np.float32).flatten()
    return _l2_normalize(arr).tolist()


def to_pgvector_literal(embedding: list[float]) -> str:
    """Render a float list as a pgvector string literal.

    pgvector accepts `"[0.012,-0.034,...]"` and casts to `vector(N)`. We
    format with a fixed precision so the text round-trips losslessly
    through `float32` (7 significant digits suffice).
    """
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


__all__ = [
    "DEFAULT_MODEL", "MODEL_REPO",
    "ClipUnavailable",
    "get_model", "embed_image", "to_pgvector_literal",
]
