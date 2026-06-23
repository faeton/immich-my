"""onnx CLIP backend (Phase 3) — Immich's own .onnx via onnxruntime.

Two tiers:
- Unit (always run): dispatch routing, config + journal version wiring, and the
  preprocessing recipe shape/normalization (no model, no network).
- Integration (model-gated): downloads Immich's `immich-app/ViT-B-32__openai`
  visual model and asserts a 512-d unit vector, determinism, and — the real
  point — CPU-vs-CoreML execution-provider parity (cosine > 0.9999), the
  guardrail that the Mac path stays in Immich's vector space. Skips cleanly when
  onnxruntime / huggingface_hub / the network aren't available.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from immy import clip as clip_mod
from immy import config as config_mod
from immy import journal as journal_mod


# --- unit: wiring (no model, no network) ---------------------------------


def test_onnx_in_known_backends():
    assert "onnx" in clip_mod.KNOWN_BACKENDS


def test_embed_dispatch_routes_onnx(tmp_path, monkeypatch):
    img = tmp_path / "preview.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    seen = {}

    def fake(image_path, *, model_name=clip_mod.DEFAULT_MODEL):
        seen["model"] = model_name
        return [0.0, 1.0]

    monkeypatch.setattr(clip_mod, "embed_image_via_onnx", fake)
    out = clip_mod.embed(img, backend="onnx", model_name="ViT-B-32__openai")
    assert out == [0.0, 1.0]
    assert seen["model"] == "ViT-B-32__openai"


def test_clip_version_onnx_is_distinct():
    # onnx shares Immich's space but is still a different producer than mlx, so
    # the journal version must differ → switching backend re-embeds.
    assert (
        journal_mod.clip_version("ViT-B-32__openai", "onnx")
        == "clip:onnx/ViT-B-32__openai"
    )
    assert journal_mod.clip_version("ViT-B-32__openai", "mlx") == "clip:ViT-B-32__openai"


def test_config_parses_clip_backend_onnx(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("ml:\n  clip_backend: onnx\n", encoding="utf-8")
    cfg = config_mod.load(p)
    assert cfg.ml is not None
    assert cfg.ml.clip_backend == "onnx"


def test_onnx_preprocess_shape_and_norm():
    """Recipe runs without the model: shortest-side resize → centre crop →
    NCHW (1,3,224,224), and the cfg's mean/std are actually applied."""
    from PIL import Image

    cfg = {
        "size": 224,
        "mean": [0.5, 0.5, 0.5],
        "std": [0.5, 0.5, 0.5],
    }
    # a non-square image to exercise resize + centre crop
    img_path = Path(_write_solid(Image, 400, 300, (255, 0, 0)))
    arr = clip_mod._onnx_preprocess(img_path, cfg)
    assert arr.shape == (1, 3, 224, 224)
    assert arr.dtype == np.float32
    # red pixel = 1.0 → (1-0.5)/0.5 = 1.0 ; green/blue 0 → -1.0
    assert arr[0, 0].mean() == pytest.approx(1.0, abs=1e-4)
    assert arr[0, 1].mean() == pytest.approx(-1.0, abs=1e-4)


def test_onnx_preprocess_reads_size_as_list():
    from PIL import Image

    cfg = {"size": [224, 224], "mean": [0.0, 0.0, 0.0], "std": [1.0, 1.0, 1.0]}
    img_path = Path(_write_solid(Image, 256, 256, (128, 128, 128)))
    arr = clip_mod._onnx_preprocess(img_path, cfg)
    assert arr.shape == (1, 3, 224, 224)


# --- integration: real Immich model (gated) ------------------------------


def _sample_jpeg() -> Path | None:
    posters = Path(
        "/Users/faeton/Media/Trips/2022-11-krakow/.audit/derivatives/_posters"
    )
    if posters.is_dir():
        for j in sorted(posters.glob("*.jpg")):
            return j
    return None


def _onnx_or_skip(providers=None):
    """Build the session or skip if onnxruntime/hf/network unavailable."""
    try:
        return clip_mod._onnx_session("ViT-B-32__openai", providers)
    except (clip_mod.ClipUnavailable, clip_mod.ClipBackendError) as e:
        pytest.skip(f"onnx backend unavailable (offline/missing dep): {e}")


def _cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


@pytest.mark.integration
def test_onnx_embedding_is_512d_unit_vector():
    img = _sample_jpeg()
    if img is None:
        pytest.skip("no sample poster JPEG on this machine")
    _onnx_or_skip()
    out = clip_mod.embed_image_via_onnx(img)
    assert len(out) == 512
    assert float(np.linalg.norm(out)) == pytest.approx(1.0, abs=1e-4)


@pytest.mark.integration
def test_onnx_is_deterministic():
    img = _sample_jpeg()
    if img is None:
        pytest.skip("no sample poster JPEG on this machine")
    _onnx_or_skip()
    a = clip_mod.embed_image_via_onnx(img)
    b = clip_mod.embed_image_via_onnx(img)
    assert _cosine(a, b) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.integration
def test_onnx_cpu_vs_coreml_ep_parity():
    """The in-space guardrail: CoreML and CPU must agree to ~0.999988 cosine
    (measured), so a Mac (CoreML) embedding shares the index with a NAS (CPU)
    embedding. Skips if CoreML EP isn't present."""
    import onnxruntime as ort

    if "CoreMLExecutionProvider" not in ort.get_available_providers():
        pytest.skip("CoreMLExecutionProvider not available")
    img = _sample_jpeg()
    if img is None:
        pytest.skip("no sample poster JPEG on this machine")
    _onnx_or_skip(["CPUExecutionProvider"])
    cpu = clip_mod.embed_image_via_onnx(img, providers=["CPUExecutionProvider"])
    coreml = clip_mod.embed_image_via_onnx(
        img, providers=["CoreMLExecutionProvider", "CPUExecutionProvider"]
    )
    assert _cosine(cpu, coreml) > 0.9999


# --- helpers -------------------------------------------------------------


def _write_solid(Image, w: int, h: int, color) -> str:
    # PNG (lossless) so a solid colour survives exactly — JPEG would shift 255
    # to ~253 and break the exact-normalization assertions.
    import tempfile

    fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    Image.new("RGB", (w, h), color).save(fd.name, "PNG")
    return fd.name
