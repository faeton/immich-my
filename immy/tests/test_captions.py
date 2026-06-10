"""Unit tests for `immy/captions.py` — request shape, response parsing,
and idempotency helpers. The HTTP call itself is mocked (we don't want
tests depending on a running LM Studio / network)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from immy import captions


def _tiny_jpeg(path: Path) -> None:
    """Write the smallest valid JPEG known to PIL/libjpeg — a 1×1 white
    pixel. Enough for _encode_image to take the fast path and skip the
    pyvips re-encode branch entirely."""
    from PIL import Image

    Image.new("RGB", (2, 2), color=(255, 255, 255)).save(path, "JPEG")


def test_format_and_detect_ai_description():
    formatted = captions.format_description("a dog on a hillside")
    assert formatted == "AI: a dog on a hillside"
    assert captions.is_ai_description(formatted)
    assert not captions.is_ai_description("a user-typed description")
    assert not captions.is_ai_description("")
    assert not captions.is_ai_description(None)


def test_is_camera_boilerplate():
    # Camera-embedded junk that Immich's metadata refresh re-imports over
    # our captions: DJI writes 'default', Insta360 a DCIM path or the
    # file's own name. All overwritable; real text never is.
    f = captions.is_camera_boilerplate
    assert f("default", "DJI_0011.JPG")
    assert f("Default", None)
    assert f("DCIM\\Camera01\\IMG_20260226_072521_00_010.ins", None)
    assert f("DCIM/Camera01/x", None)
    assert f("IMG_20250502_095157_00_006.insp", "IMG_20250502_095157_00_006.insp")
    assert f("IMG_20250502_095157_00_006", "IMG_20250502_095157_00_006.insp")
    assert not f("a user-typed description", "DJI_0011.JPG")
    assert not f("AI: a dog on a hillside", "DJI_0011.JPG")
    assert not f("", "DJI_0011.JPG")
    assert not f(None, None)


def test_encode_image_passes_jpeg_through(tmp_path: Path):
    src = tmp_path / "sample.jpg"
    _tiny_jpeg(src)
    uri = captions._encode_image(src)
    assert uri.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(uri.split(",", 1)[1])
    # JPEG magic: FF D8 FF — verifies we emitted the bytes verbatim
    # rather than silently routing through a re-encode path.
    assert raw[:3] == b"\xff\xd8\xff"


def test_caption_request_shape_and_response_parse(tmp_path: Path):
    src = tmp_path / "sample.jpg"
    _tiny_jpeg(src)
    cfg = captions.CaptionerConfig(
        endpoint="http://example.invalid/v1",
        model="fake-vlm",
        api_key="sk-test",
        prompt="describe",
        max_tokens=42,
    )
    fake_response = {
        "model": "fake-vlm",
        "choices": [{"message": {"content": "  a tiny white square  "}}],
        "usage": {"prompt_tokens": 1050, "completion_tokens": 9},
    }
    captured: dict = {}

    def fake_post(url, payload, *, api_key, timeout_s):
        captured["url"] = url
        captured["payload"] = payload
        captured["api_key"] = api_key
        return fake_response

    with patch.object(captions, "_post_json", side_effect=fake_post):
        result = captions.caption(src, config=cfg)

    assert result.text == "a tiny white square"
    assert result.model == "fake-vlm"
    assert result.prompt_tokens == 1050
    assert result.completion_tokens == 9

    assert captured["url"] == "http://example.invalid/v1/chat/completions"
    assert captured["api_key"] == "sk-test"
    payload = captured["payload"]
    assert payload["model"] == "fake-vlm"
    assert payload["max_tokens"] == 42
    content = payload["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


def test_caption_rejects_empty_model_output(tmp_path: Path):
    src = tmp_path / "sample.jpg"
    _tiny_jpeg(src)
    cfg = captions.CaptionerConfig(endpoint="http://example.invalid/v1")
    empty = {"choices": [{"message": {"content": "  "}}]}
    with patch.object(captions, "_post_json", return_value=empty):
        with pytest.raises(captions.CaptionError):
            captions.caption(src, config=cfg)


def test_caption_retries_invalid_image_with_reencode(tmp_path: Path):
    """A staged preview whose byte stream the server can't decode (LM
    Studio: HTTP 400 `Invalid image detected at index 0`) is retried
    once with a pyvips decode→re-encode — recoverable pixels in a
    damaged container shouldn't fail the asset."""
    pytest.importorskip("pyvips")
    src = tmp_path / "preview.jpeg"
    _tiny_jpeg(src)

    cfg = captions.CaptionerConfig(endpoint="http://example.invalid/v1")
    ok = {"choices": [{"message": {"content": "recovered"}}], "usage": {}}
    sent_uris: list[str] = []

    def fake_post(url, payload, *, api_key, timeout_s):
        sent_uris.append(
            payload["messages"][0]["content"][1]["image_url"]["url"]
        )
        if len(sent_uris) == 1:
            raise captions.CaptionError(
                "HTTP 400 from http://example.invalid/v1/chat/completions:"
                ' {"error":"Invalid image detected at index 0 "}'
            )
        return ok

    with patch.object(captions, "_post_json", side_effect=fake_post):
        result = captions.caption(src, config=cfg)

    assert result.text == "recovered"
    assert len(sent_uris) == 2
    # The retry must send different bytes — a re-encoded stream, not the
    # same verbatim payload the server already rejected.
    assert sent_uris[0] != sent_uris[1]
    assert sent_uris[1].startswith("data:image/jpeg;base64,")


def test_caption_does_not_retry_other_errors(tmp_path: Path):
    """Auth/model/transport failures aren't image problems — no retry."""
    src = tmp_path / "preview.jpeg"
    _tiny_jpeg(src)
    cfg = captions.CaptionerConfig(endpoint="http://example.invalid/v1")
    boom = captions.CaptionError("HTTP 500 from x: model crashed")
    with patch.object(
        captions, "_post_json", side_effect=boom,
    ) as posted:
        with pytest.raises(captions.CaptionError):
            captions.caption(src, config=cfg)
    assert posted.call_count == 1


def test_caption_does_not_retry_when_already_reencoded(tmp_path: Path):
    """A non-JPEG source already went through the pyvips re-encode on
    the first attempt — a retry would send identical bytes, so an
    invalid-image rejection is terminal."""
    pytest.importorskip("pyvips")
    src = tmp_path / "original.png"
    from PIL import Image

    Image.new("RGB", (2, 2), color=(255, 255, 255)).save(src, "PNG")
    cfg = captions.CaptionerConfig(endpoint="http://example.invalid/v1")
    boom = captions.CaptionError(
        'HTTP 400 from x: {"error":"Invalid image detected at index 0 "}'
    )
    with patch.object(
        captions, "_post_json", side_effect=boom,
    ) as posted:
        with pytest.raises(captions.CaptionError):
            captions.caption(src, config=cfg)
    assert posted.call_count == 1


def test_caption_prefers_preview_when_available(tmp_path: Path):
    """When a staged preview JPEG exists it's used as the caption source
    — saves the pyvips re-encode we'd otherwise pay on HEIC/RAW originals
    and guarantees every backend sees an identical image."""
    original = tmp_path / "IMG_0001.heic"
    original.write_bytes(b"not-a-real-heic-but-we-never-read-it")
    preview = tmp_path / "preview.jpeg"
    _tiny_jpeg(preview)

    cfg = captions.CaptionerConfig(endpoint="http://example.invalid/v1")
    fake_response = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {},
    }
    with patch.object(
        captions, "_post_json", return_value=fake_response,
    ) as posted:
        captions.caption(original, config=cfg, preview=preview)
    # The data URI in the payload must reflect the preview bytes, not
    # the original — confirms we bypassed the pyvips branch entirely.
    uri = posted.call_args.args[1]["messages"][0]["content"][1]["image_url"]["url"]
    preview_b64 = base64.b64encode(preview.read_bytes()).decode("ascii")
    assert uri == f"data:image/jpeg;base64,{preview_b64}"
