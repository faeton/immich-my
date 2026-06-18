"""Regression tests for MLConfig loading — esp. the Phase 1 fix where an
`[ml]` block without `clip_model` must still yield an MLConfig (transcript/
caption-only NAS deployments delegate CLIP to Immich's ML server).
"""

from __future__ import annotations

from pathlib import Path

from immy import config as config_mod


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(body, encoding="utf-8")
    return p


def test_ml_built_without_clip_model(tmp_path):
    cfg = config_mod.load(_write(tmp_path, """
ml:
  whisper_backend: whispercpp
  whisper_endpoint: http://n5:8080/inference
  whisper_model: large-v3
"""))
    assert cfg.ml is not None
    assert cfg.ml.whisper_backend == "whispercpp"
    assert cfg.ml.whisper_endpoint == "http://n5:8080/inference"
    assert cfg.ml.whisper_model == "large-v3"
    assert cfg.ml.clip_model is None


def test_ml_clip_only_still_defaults_backend_to_mlx(tmp_path):
    cfg = config_mod.load(_write(tmp_path, """
ml:
  clip_model: ViT-B-32__openai
"""))
    assert cfg.ml is not None
    assert cfg.ml.clip_model == "ViT-B-32__openai"
    assert cfg.ml.whisper_backend == "mlx"


def test_no_ml_block_yields_none(tmp_path):
    cfg = config_mod.load(_write(tmp_path, "originals_root: /tmp/x\n"))
    assert cfg.ml is None
