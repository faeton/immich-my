"""immich-ml CLIP backend (Phase 3) — unit tests with a mocked /predict.

The Immich v2.7.5 contract: `response["clip"]` is a JSON *string* (double
encoded) of the 512-float embedding. These tests mock urlopen so they run
without a server; the live contract is verified separately against the NAS.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from immy import clip as clip_mod
from immy import config as config_mod
from immy import journal as journal_mod


class _FakeResp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _img(tmp_path: Path) -> Path:
    p = tmp_path / "preview.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    return p


def test_immich_ml_parses_double_encoded_clip(tmp_path, monkeypatch):
    vec = [3.0, 4.0] + [0.0] * 510            # norm 5 → normalized to 0.6,0.8
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = req.data
        # v2.7.x stringifies the embedding inside the JSON response
        return _FakeResp({"clip": json.dumps(vec),
                          "imageHeight": 10, "imageWidth": 20})

    monkeypatch.setattr(clip_mod.urllib.request, "urlopen", fake_urlopen)
    out = clip_mod.embed_image_via_immich_ml(
        _img(tmp_path), model_name="ViT-B-32__openai",
        endpoint="http://n5:3003",
    )
    assert len(out) == 512
    assert out[0] == pytest.approx(0.6)
    assert out[1] == pytest.approx(0.8)
    # request shape: hits /predict, carries the entries pipeline request + file
    assert captured["url"] == "http://n5:3003/predict"
    assert b'name="entries"' in captured["body"]
    assert b'"modelName": "ViT-B-32__openai"' in captured["body"]
    assert b'name="image"' in captured["body"]
    assert b"fakejpeg" in captured["body"]


def test_immich_ml_tolerates_plain_list(tmp_path, monkeypatch):
    vec = [1.0] + [0.0] * 511
    monkeypatch.setattr(
        clip_mod.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResp({"clip": vec}),
    )
    out = clip_mod.embed_image_via_immich_ml(
        _img(tmp_path), endpoint="http://n5:3003")
    assert len(out) == 512 and out[0] == pytest.approx(1.0)


def test_immich_ml_raises_on_missing_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        clip_mod.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResp({"imageHeight": 1}),
    )
    with pytest.raises(clip_mod.ClipBackendError):
        clip_mod.embed_image_via_immich_ml(
            _img(tmp_path), endpoint="http://n5:3003")


def test_immich_ml_raises_on_transport_error(tmp_path, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(clip_mod.urllib.request, "urlopen", boom)
    with pytest.raises(clip_mod.ClipBackendError):
        clip_mod.embed_image_via_immich_ml(
            _img(tmp_path), endpoint="http://n5:3003")


def test_embed_dispatch_immich_ml_requires_endpoint(tmp_path):
    with pytest.raises(clip_mod.ClipBackendError):
        clip_mod.embed(_img(tmp_path), backend="immich-ml", endpoint=None)


def test_embed_dispatch_unknown_backend(tmp_path):
    with pytest.raises(clip_mod.ClipBackendError):
        clip_mod.embed(_img(tmp_path), backend="nope")


def test_clip_version_backend_aware():
    # mlx keeps the bare form (no Mac journal churn); others are distinct
    assert journal_mod.clip_version("ViT-B-32__openai") == "clip:ViT-B-32__openai"
    assert journal_mod.clip_version("ViT-B-32__openai", "mlx") == "clip:ViT-B-32__openai"
    assert (
        journal_mod.clip_version("ViT-B-32__openai", "immich-ml")
        == "clip:immich-ml/ViT-B-32__openai"
    )


def test_config_parses_clip_backend(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(
        "ml:\n  clip_backend: immich-ml\n  immich_ml_url: http://n5:3003\n",
        encoding="utf-8",
    )
    cfg = config_mod.load(p)
    assert cfg.ml is not None
    assert cfg.ml.clip_backend == "immich-ml"
    assert cfg.ml.immich_ml_url == "http://n5:3003"


def test_config_clip_backend_defaults_mlx(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("ml:\n  clip_model: ViT-B-32__openai\n", encoding="utf-8")
    cfg = config_mod.load(p)
    assert cfg.ml is not None
    assert cfg.ml.clip_backend == "mlx"
    assert cfg.ml.immich_ml_url is None
