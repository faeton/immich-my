"""Phase 3b — VLM captions for image assets.

Speaks the OpenAI chat-completions shape, which every serious vision API
has converged on: OpenAI, Anthropic (via their OpenAI-compat endpoint),
Google Gemini (via their OpenAI-compat endpoint), Groq, Together,
OpenRouter, plus local runners (LM Studio, Ollama). One code path,
config-only backend swap.

Request payload we send:

    POST {endpoint}/chat/completions
    Authorization: Bearer {api_key}   # omitted if no key configured
    {
        "model": "<config.model>",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "<config.prompt>"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,..."}},
            ],
        }],
        "max_tokens": <config.max_tokens>,
    }

We feed the 1440 px preview JPEG (already produced by `derivatives.py`)
when available — a full 24 MP original is wasted bytes on the wire and
most VLMs downscale to ~1024 px internally anyway. When the preview
isn't staged, we pyvips-resize the original in memory to 1440 px long
edge and JPEG-encode at Q80. Same spec as the on-disk preview, just
never touches disk.

Captions are written to `asset_exif.description` with an `AI: ` prefix
so they're visually distinct from human-typed descriptions and from
Whisper transcripts (which go in as-is). Existing non-empty,
non-`AI:`-prefixed descriptions are never clobbered — user text wins.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://localhost:1234/v1"  # LM Studio default
DEFAULT_MODEL = "mlx-qwopus3.5-27b-v3-vision"
# When pointing at LM Studio with no explicit model configured, we ask
# its REST API which model is currently loaded and pick from this list
# in order — saves the user from juggling config every time they swap
# models in the GUI. Both winners of raw/vlm-bench/ (2026-04-26):
# qwopus is best on quality / OCR, gemma-31b is the fast fallback for
# very large batches. If neither is loaded, we walk to "any loaded VLM"
# inside detect_lm_studio_model, then to LM_STUDIO_FALLBACK_MODEL.
LM_STUDIO_PREFERRED_MODELS: tuple[str, ...] = (
    "mlx-qwopus3.5-27b-v3-vision",
    "gemma-4-31b-it",
)
LM_STUDIO_FALLBACK_MODEL = LM_STUDIO_PREFERRED_MODELS[0]
# Reasoning-capable VLMs (Gemma 4, DeepSeek-VL, GPT-5 family) spend
# 200-400 tokens "thinking" before emitting the user-visible answer,
# and LM Studio reports that via `reasoning_content` / the
# `completion_tokens_details.reasoning_tokens` field. `max_tokens` is a
# ceiling, not a consumption target — non-reasoning VLMs (moondream,
# Qwen2.5-VL-7B, gpt-4o-mini, gemini-flash) only emit the caption and
# bill only for what they actually produce. So a generous default
# covers both camps; lower it in config if you're paying per-token and
# the model never reasons.
DEFAULT_MAX_TOKENS = 1024
DEFAULT_PROMPT = (
    "Describe this photo in one short sentence. Focus on subjects, "
    "setting, and any visible text. Do not speculate about context "
    "that isn't in the frame. Answer directly; do not think step by "
    "step."
)
DEFAULT_TIMEOUT_S = 60.0
AI_PREFIX = "AI: "

# Caption on the preview (1440 px JPEG) when staged; otherwise resize
# the original to this long edge in memory. Matches derivatives.py so a
# cached preview and an on-the-fly resize produce identical pixels.
PREVIEW_LONG_EDGE_PX = 1440
PREVIEW_JPEG_QUALITY = 80


class CaptionError(RuntimeError):
    """Raised for transport / HTTP / parse failures the caller may
    choose to swallow (`on_caption_error='skip'`) or re-raise."""


@dataclass(frozen=True)
class CaptionResult:
    text: str       # already AI_PREFIX-stripped; raw model output, trimmed
    model: str
    # Token counts from the provider's `usage` block when present.
    # Local runners sometimes omit it; None means "unknown, not zero".
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True)
class CaptionerConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    prompt: str = DEFAULT_PROMPT
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    # Extra top-level fields merged verbatim into the request payload.
    # Provider-specific knobs that don't fit the common shape live here so
    # the default (LM Studio / OpenAI) path stays byte-identical when unset.
    # The one that matters on the N5: Ollama serves gemma4 with thinking on
    # by default, which dumps the answer into a `reasoning` field and leaves
    # `content` empty — `{"reasoning_effort": "none"}` restores plain content.
    extra_body: dict[str, Any] | None = None


def _encode_image(source: Path, *, force_reencode: bool = False) -> str:
    """Return a `data:image/jpeg;base64,...` URI.

    If `source` is already a JPEG we send its bytes verbatim — no re-encode
    tax on the preview files `derivatives.py` already staged. Otherwise
    (original is a HEIC/RAW/PNG and we don't have a staged preview) we
    pyvips-resize to PREVIEW_LONG_EDGE_PX and JPEG-encode in memory.

    `force_reencode` routes a JPEG through the pyvips branch anyway —
    the recovery path for staged previews whose byte stream is damaged
    (e.g. a non-truncating double write) but whose pixels still decode:
    one decode→re-encode round-trip yields a clean stream.
    """
    suffix = source.suffix.lower()
    if suffix in (".jpg", ".jpeg") and not force_reencode:
        data = source.read_bytes()
    else:
        # Silence libvips warnings before the (lazy) import; see the same
        # block at the top of derivatives.py for details.
        os.environ.setdefault("VIPS_WARNING", "0")
        os.environ.setdefault("G_MESSAGES_DEBUG", "")
        import logging as _logging
        _logging.getLogger("pyvips").setLevel(_logging.WARNING)
        import pyvips  # lazy — heavy import, not needed in pure-unit tests

        image = pyvips.Image.thumbnail(
            str(source), PREVIEW_LONG_EDGE_PX, height=PREVIEW_LONG_EDGE_PX,
            size="down",
        )
        data = image.jpegsave_buffer(Q=PREVIEW_JPEG_QUALITY, interlace=True)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def detect_lm_studio_model(
    endpoint: str,
    *,
    timeout_s: float = 5.0,
) -> str | None:
    """Return the id of a currently-loaded LM Studio model, or None.

    Hits LM Studio's REST API (`/api/v0/models`) which exposes a `state`
    field per model; the OpenAI-compat `/v1/models` does not. Walks
    `LM_STUDIO_PREFERRED_MODELS` in order first (so a loaded captioner-
    grade model wins over an unrelated VLM left running for some other
    app), then falls back to any loaded VLM, then any loaded model.
    Returns None if the endpoint isn't LM Studio, nothing is loaded, or
    the call fails — the caller is expected to fall back to a hard-coded
    default.
    """
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base.rstrip('/')}/api/v0/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, OSError):
        return None
    items = body.get("data") or []
    loaded = [m for m in items if m.get("state") == "loaded"]
    loaded_ids = {m.get("id") for m in loaded}
    for pref in LM_STUDIO_PREFERRED_MODELS:
        if pref in loaded_ids:
            return pref
    vlms = [m for m in loaded if m.get("type") == "vlm"]
    pick = vlms[0] if vlms else (loaded[0] if loaded else None)
    return str(pick["id"]) if pick and pick.get("id") else None


def _post_json(
    url: str,
    payload: dict,
    *,
    api_key: str | None,
    timeout_s: float,
) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Surface the server's error body — most providers return JSON
        # with a useful `error.message` we want in the logs.
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise CaptionError(f"HTTP {e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise CaptionError(f"connection to {url} failed: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise CaptionError(f"non-JSON response from {url}: {e}") from e


def _is_invalid_image_rejection(e: CaptionError) -> bool:
    """True when the server refused the request because it couldn't
    decode the image itself (LM Studio: HTTP 400 `Invalid image detected
    at index N`) — as opposed to auth, model, or transport failures."""
    msg = str(e).lower()
    return "http 400" in msg and "invalid image" in msg


def caption(
    media: Path,
    *,
    config: CaptionerConfig,
    preview: Path | None = None,
) -> CaptionResult:
    """Generate a caption for one image asset.

    `preview` is the staged 1440 px JPEG from `derivatives.py` — pass it
    when you have it to skip the in-memory pyvips re-encode. Falls back
    to `media` when None.
    """
    source = preview if preview is not None and preview.is_file() else media
    # JPEGs go over the wire verbatim — which also means a damaged byte
    # stream goes over verbatim. Tracked so the retry below knows a
    # re-encode would actually send different bytes.
    sent_verbatim = source.suffix.lower() in (".jpg", ".jpeg")
    image_data_uri = _encode_image(source)

    url = config.endpoint.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": config.prompt},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ],
        }],
        "max_tokens": config.max_tokens,
    }
    if config.extra_body:
        payload.update(config.extra_body)
    try:
        response = _post_json(
            url, payload,
            api_key=config.api_key,
            timeout_s=config.timeout_s,
        )
    except CaptionError as e:
        # LM Studio's image decoder is strict; PIL/libvips are lenient.
        # A staged preview with a damaged byte stream (premature EOI,
        # stale tail from a non-truncating double write) decodes fine
        # everywhere except here. The pixels are still recoverable, so
        # retry once with a decode→re-encode round-trip before giving up.
        if not (sent_verbatim and _is_invalid_image_rejection(e)):
            raise
        payload["messages"][0]["content"][1]["image_url"]["url"] = (
            _encode_image(source, force_reencode=True)
        )
        response = _post_json(
            url, payload,
            api_key=config.api_key,
            timeout_s=config.timeout_s,
        )

    try:
        message = response["choices"][0]["message"]
        text = message["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise CaptionError(f"unexpected response shape: {response}") from e
    text = str(text or "").strip()
    if not text:
        # The reasoning-leak trap: a model with thinking on (e.g. gemma4 on
        # Ollama's /v1) returns its answer in a `reasoning` field and leaves
        # `content` empty. Diagnose it specifically — the fix is config, not
        # code (set captioner.extra_body: {reasoning_effort: none}).
        if isinstance(message, dict) and str(message.get("reasoning") or "").strip():
            raise CaptionError(
                "empty caption: model put its answer in a 'reasoning' field "
                "(thinking enabled). Set captioner.extra_body: "
                "{reasoning_effort: none} for this endpoint."
            )
        raise CaptionError("empty caption in model response")

    usage = response.get("usage") or {}
    return CaptionResult(
        text=text,
        model=str(response.get("model") or config.model),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


def format_description(caption_text: str) -> str:
    """Prefix a raw caption for storage in `asset_exif.description`.
    The `AI: ` sentinel lets later passes (and humans) tell model output
    apart from user-typed text and Whisper excerpts."""
    return f"{AI_PREFIX}{caption_text}"


def is_ai_description(description: str | None) -> bool:
    """True when a DB description was written by this captioner (and is
    therefore safe to overwrite on re-runs with a better model)."""
    return bool(description) and description.startswith(AI_PREFIX)


def is_camera_boilerplate(description: str | None, file_name: str | None = None) -> bool:
    """True for descriptions the *camera* embedded in the file — junk that
    Immich's metadata refresh re-imports over our captions (2026-06: 338
    assets clobbered after a library scan). DJI writes the literal
    'default', Insta360 writes a DCIM path or the file's own name. These
    are always safe to overwrite; they carry zero information.
    """
    d = (description or "").strip()
    if not d:
        return False
    if d.lower() == "default":
        return True
    if d.startswith(("DCIM\\", "DCIM/")):
        return True
    if file_name:
        stem = file_name.rsplit(".", 1)[0]
        if d == file_name or d == stem:
            return True
    return False


__all__ = [
    "AI_PREFIX",
    "DEFAULT_ENDPOINT", "DEFAULT_MODEL", "DEFAULT_PROMPT", "DEFAULT_MAX_TOKENS",
    "LM_STUDIO_PREFERRED_MODELS", "LM_STUDIO_FALLBACK_MODEL",
    "CaptionError", "CaptionResult", "CaptionerConfig",
    "caption", "detect_lm_studio_model",
    "format_description", "is_ai_description", "is_camera_boilerplate",
]
