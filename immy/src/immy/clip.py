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

import json
import mimetypes
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_MODEL = "ViT-B-32__openai"
DEFAULT_BACKEND = "mlx"            # mlx (Apple) | immich-ml (NAS HTTP) | onnx
KNOWN_BACKENDS = ("mlx", "immich-ml", "onnx")

# mlx-clip's `mlx_clip(model_dir)` interprets its first arg as a local path
# and downloads/converts weights *into that path* if missing. Default to a
# user-level cache so repeated runs from any CWD share one copy (~600 MB
# per model). Override with IMMY_CLIP_CACHE.
_CLIP_CACHE_ROOT = Path(
    os.environ.get("IMMY_CLIP_CACHE")
    or (Path.home() / ".cache" / "mlx-clip")
)

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

# ONNX backend cache. Immich publishes the exact `.onnx` it serves under the
# `immich-app/<model_name>` HF repo; running that file through onnxruntime lands
# embeddings in Immich's OWN vector space (CPU↔CoreML parity ~0.999988 cosine),
# unlike the mlx reimplementation (~0.925, different weights + quick_gelu +
# Metal kernels) which would split the shared smart_search index. Use `onnx`
# for anything that writes to smart_search; reuse one session per process.
_ONNX_CACHE_ROOT = Path(
    os.environ.get("IMMY_CLIP_ONNX_CACHE")
    or (Path.home() / ".cache" / "immy-clip-onnx")
)
_ONNX_SESSION_CACHE: dict[str, Any] = {}


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
    repo = _repo_for(model_name)
    cache_dir = _CLIP_CACHE_ROOT / repo
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    model = _mlx_clip(str(cache_dir), hf_repo=repo)
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


class ClipBackendError(RuntimeError):
    """An HTTP CLIP backend (immich-ml) was unreachable or returned a shape we
    couldn't parse. Distinct from `ClipUnavailable` (mlx/model not installed)."""


def embed_image_via_immich_ml(
    image_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    endpoint: str,
    timeout_s: float = 120.0,
) -> list[float]:
    """L2-normalized CLIP image embedding from an Immich ML server.

    POSTs the preview JPEG to `{endpoint}/predict` (multipart) with the
    Immich pipeline-request shape and parses the visual embedding back.

    Contract verified against Immich v2.7.5 (2026-06-18):
      request : entries=`{"clip":{"visual":{"modelName":<m>,"options":{}}}}`
                + image=<bytes>
      response: {"clip": "<json-string of [float,...]>", "imageHeight",
                "imageWidth"} — note `clip` is a JSON STRING that needs a
                second parse; the vector is already unit-norm but we
                re-normalize defensively to match the mlx path exactly.
    """
    entries = json.dumps(
        {"clip": {"visual": {"modelName": model_name, "options": {}}}}
    )
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    ctype = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    body = crlf.join([
        f"--{boundary}".encode(),
        b'Content-Disposition: form-data; name="entries"',
        b"", entries.encode("utf-8"),
        f"--{boundary}".encode(),
        (
            'Content-Disposition: form-data; name="image"; '
            f'filename="{image_path.name}"'
        ).encode(),
        f"Content-Type: {ctype}".encode(),
        b"", image_path.read_bytes(),
        f"--{boundary}--".encode(), b"",
    ])
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/predict",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # URLError, timeout, JSON decode
        raise ClipBackendError(
            f"immich-ml /predict at {endpoint} failed: {e}"
        ) from e
    clip = payload.get("clip")
    # `clip` is double-encoded (a JSON string) on v2.7.x; tolerate a plain
    # list too in case a future version stops stringifying it.
    if isinstance(clip, str):
        try:
            clip = json.loads(clip)
        except ValueError as e:
            raise ClipBackendError(
                f"immich-ml returned unparseable clip string: {clip[:80]!r}"
            ) from e
    if not isinstance(clip, list) or not clip:
        raise ClipBackendError(
            f"immich-ml response missing clip embedding: {str(payload)[:120]}"
        )
    arr = np.asarray(clip, dtype=np.float32).flatten()
    return _l2_normalize(arr).tolist()


def _onnx_default_providers() -> list[str]:
    """CoreML first on Apple Silicon, CPU fallback. CPU-only on machines
    without the CoreML EP (e.g. the NAS) — both produce the same vectors."""
    import onnxruntime as ort

    avail = ort.get_available_providers()
    providers = []
    if "CoreMLExecutionProvider" in avail:
        providers.append("CoreMLExecutionProvider")
    providers.append("CPUExecutionProvider")
    return providers


def _onnx_assets(model_name: str) -> tuple[Path, Path]:
    """Fetch Immich's visual `model.onnx` + `preprocess_cfg.json` into the
    local cache (reused across runs). `snapshot_download` pulls the whole
    `visual/` folder so any external-weights sidecar comes along too.
    """
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError as e:
        raise ClipUnavailable(
            "huggingface_hub is required for clip_backend 'onnx'; "
            "`uv add huggingface_hub`"
        ) from e
    repo = f"immich-app/{model_name}"
    try:
        snap = snapshot_download(
            repo, allow_patterns=["visual/*"], cache_dir=str(_ONNX_CACHE_ROOT)
        )
    except Exception as e:  # network, auth, missing repo
        raise ClipBackendError(
            f"failed to download Immich ONNX model {repo!r}: {e}"
        ) from e
    visual = Path(snap) / "visual"
    model_path, cfg_path = visual / "model.onnx", visual / "preprocess_cfg.json"
    if not model_path.exists() or not cfg_path.exists():
        raise ClipBackendError(
            f"{repo!r} is missing visual/model.onnx or preprocess_cfg.json"
        )
    return model_path, cfg_path


def _onnx_session(model_name: str, providers: list[str] | None) -> Any:
    """Cached `(session, input_name, cfg)` per (model, providers). Lazy-imports
    onnxruntime so `import immy.clip` works without it installed."""
    providers = providers or _onnx_default_providers()
    key = f"{model_name}|{','.join(providers)}"
    cached = _ONNX_SESSION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise ClipUnavailable(
            "onnxruntime is required for clip_backend 'onnx'; "
            "`uv add onnxruntime`"
        ) from e
    model_path, cfg_path = _onnx_assets(model_name)
    cfg = json.loads(cfg_path.read_text())
    session = ort.InferenceSession(str(model_path), providers=providers)
    # Visual encoder must expose the single image-embedding output. If a future
    # Immich model swap changes the graph shape, fail loudly here rather than
    # silently embedding the wrong tensor into smart_search.
    outs = session.get_outputs()
    if outs[0].name != "embedding":
        raise ClipBackendError(
            f"unexpected ONNX output {outs[0].name!r} for {model_name!r}; "
            "expected 'embedding' — model graph changed?"
        )
    entry = (session, session.get_inputs()[0].name, cfg)
    _ONNX_SESSION_CACHE[key] = entry
    return entry


def _onnx_preprocess(image_path: Path, cfg: dict) -> np.ndarray:
    """Replicate Immich's CLIP visual preprocessing (machine-learning
    `transforms.py`): resize the SHORTEST side to `size` (BICUBIC, integer
    truncation), centre-crop `size×size`, scale to [0,1], normalize per
    channel with the cfg's mean/std, → float32 NCHW `(1,3,size,size)`. The
    mean/std come from `preprocess_cfg.json`, not hardcoded, so a model swap
    can't silently drift the recipe.
    """
    from PIL import Image

    size = cfg.get("size", 224)
    if isinstance(size, (list, tuple)):
        size = size[0]
    size = int(size)
    mean = np.asarray(cfg["mean"], dtype=np.float32)
    std = np.asarray(cfg["std"], dtype=np.float32)

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    if w <= h:  # shortest side → size, keep aspect (Immich uses int() truncation)
        new_size = (size, int(h / w * size))
    else:
        new_size = (int(w / h * size), size)
    img = img.resize(new_size, resample=Image.Resampling.BICUBIC)
    w, h = img.size
    left, upper = (w - size) // 2, (h - size) // 2
    img = img.crop((left, upper, left + size, upper + size))

    arr = (np.asarray(img, dtype=np.float32) / 255.0 - mean) / std  # HWC
    arr = arr.transpose(2, 0, 1)                                    # CHW
    return arr[np.newaxis, ...].astype(np.float32)


def embed_image_via_onnx(
    image_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    providers: list[str] | None = None,
) -> list[float]:
    """L2-normalized CLIP image embedding from Immich's own ONNX model.

    Same `.onnx` + preprocessing Immich runs server-side, so the vector lands
    in Immich's `smart_search` space — safe to share one HNSW index. `providers`
    overrides the execution-provider list (used by the EP-parity guardrail
    test); default is CoreML→CPU.
    """
    session, input_name, cfg = _onnx_session(model_name, providers)
    pixel_values = _onnx_preprocess(image_path, cfg)
    outputs = session.run(None, {input_name: pixel_values})
    arr = np.asarray(outputs[0], dtype=np.float32).flatten()
    return _l2_normalize(arr).tolist()


def embed(
    image_path: Path,
    *,
    model_name: str = DEFAULT_MODEL,
    backend: str = DEFAULT_BACKEND,
    endpoint: str | None = None,
) -> list[float]:
    """Backend dispatch for a single image embedding.

    "mlx" (default, Apple Silicon, in-process) | "onnx" (Immich's own .onnx via
    onnxruntime, in-process, in Immich's vector space) | "immich-ml" (HTTP to an
    Immich ML server at `endpoint`). `mlx` is NOT interchangeable with the other
    two — a reimplementation of the "same" model produces different vectors —
    while `onnx` and `immich-ml` run Immich's exact model and DO share a space.
    The journal version encodes the backend (see `journal.clip_version`) so
    switching backends re-embeds. NB: the journal re-embed is per-asset, but the
    CLI skips whole "fully cached" trips before processing — so a `mlx → onnx`
    switch needs a one-time `immy process --force --with-clip` sweep across all
    trips, else mlx and onnx vectors coexist in one pgvector index and cosine
    search is inconsistent until every trip is re-run.
    """
    if backend == "mlx":
        return embed_image(image_path, model_name)
    if backend == "onnx":
        return embed_image_via_onnx(image_path, model_name=model_name)
    if backend == "immich-ml":
        if not endpoint:
            raise ClipBackendError(
                "clip_backend 'immich-ml' needs ml.immich_ml_url "
                "(the Immich ML server URL, e.g. http://n5:3003)"
            )
        return embed_image_via_immich_ml(
            image_path, model_name=model_name, endpoint=endpoint,
        )
    raise ClipBackendError(
        f"unknown clip_backend {backend!r}; expected one of {KNOWN_BACKENDS}"
    )


def to_pgvector_literal(embedding: list[float]) -> str:
    """Render a float list as a pgvector string literal.

    pgvector accepts `"[0.012,-0.034,...]"` and casts to `vector(N)`. We
    format with a fixed precision so the text round-trips losslessly
    through `float32` (7 significant digits suffice).
    """
    return "[" + ",".join(f"{x:.7g}" for x in embedding) + "]"


__all__ = [
    "DEFAULT_MODEL", "DEFAULT_BACKEND", "KNOWN_BACKENDS", "MODEL_REPO",
    "ClipUnavailable", "ClipBackendError",
    "get_model", "embed_image", "embed_image_via_immich_ml",
    "embed_image_via_onnx", "embed",
    "to_pgvector_literal",
]
