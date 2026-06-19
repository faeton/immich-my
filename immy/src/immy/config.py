"""User-level `immy` config loader.

Resolution order:
1. explicit path passed to `load(path=...)`
2. `$IMMY_CONFIG` env var
3. `~/.immy/config.yml`

Shape:

    originals_root: /mnt/incoming/originals-test
    state_root: /scratch/immy-state  # optional; where `process` writes journal/
                                     # marker/heartbeat/staged-derivatives/offline
                                     # cache. Unset → `<trip>/.audit/` (Mac path).
                                     # Set on the NAS so originals can be :ro.
                                     # Env override: IMMY_STATE_ROOT.
    sidecars_root: /library/sidecars # optional; where `.srt`/`.xmp` sidecars go.
                                     # Unset → next to the media (Mac path).
                                     # Env override: IMMY_SIDECARS_ROOT.
    notes_filename: TRIP.md          # optional; used by notes.py too
    immich:                          # optional; promote skips API if missing
      url: https://nas-media.example.ts.net:2283
      api_key: xxx
      library_id: <uuid>
    pg:                              # optional; `immy process` needs it
      host: 100.64.0.10
      port: 15432
      user: postgres
      password: xxx
      database: immich
    media:                           # optional; Y.2 derivatives need it
      host_root: /volume1/media-catalog/library  # rsync destination (NAS-side)
      container_root: /data                       # IMMICH_MEDIA_LOCATION in the container
    ml:                              # optional; Y.3 CLIP defaults
      clip_model: ViT-B-32__openai   # must match Immich's configured model
      clip_backend: mlx              # mlx (Apple) | immich-ml (NAS HTTP)
      immich_ml_url: http://n5:3003  # required when clip_backend: immich-ml
      whisper_prompt: "English, Russian, Ukrainian."  # biases auto-detect
      captioner:                     # optional; Phase 3b VLM captions
        endpoint: http://localhost:1234/v1   # LM Studio default; OpenAI/
                                             # Anthropic/Gemini via their
                                             # compat URLs
        model: qwen2.5-vl-7b-instruct
        api_key_env: OPENAI_API_KEY  # env-var *name*, not the key value
        prompt: "Describe this photo in one short sentence."
        max_tokens: 80
        extra_body:                  # merged verbatim into the request;
          reasoning_effort: none     # Ollama gemma4 needs this or content
                                     # comes back empty (answer goes to a
                                     # `reasoning` field instead)

Missing config file is not an error for `audit`; `promote` checks what it
needs and raises a clear message if `originals_root` is absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path.home() / ".immy" / "config.yml"


@dataclass(frozen=True)
class ImmichConfig:
    url: str
    api_key: str
    library_id: str


@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class MediaConfig:
    """Where Immich stores derivatives, on two sides of the container bind.

    - `host_root` is what `rsync` pushes into (e.g. `/volume1/media-catalog/library`
      mounted over SMB, or `user@host:/volume1/...` when remote). The NAS
      writes these files; the container reads them.
    - `container_root` is the same tree as Immich's `IMMICH_MEDIA_LOCATION`
      sees it (default `/data` in our compose). This is the prefix we bake
      into `asset_file.path` so Immich's server can find what we wrote.
    """

    host_root: str
    container_root: str


@dataclass(frozen=True)
class MLConfig:
    """ML model selection for Y.3+. `clip_model` must match the model
    Immich has configured (default `ViT-B-32__openai`, 512-dim) — a
    mismatch would produce vectors that pgvector refuses to insert into
    `smart_search.embedding`.

    `whisper_model` overrides the default `mlx-community/whisper-large-v3-mlx`
    used by `--with-transcripts`. Any HF repo id mlx-whisper understands
    is fine; leave None to use the default.

    `whisper_prompt` is passed to Whisper as `initial_prompt`. A short
    phrase in the languages you expect (e.g. "English, Russian, Ukrainian.")
    biases auto-detection and tokenisation toward those — handy when the
    typical clip is one of a small set but you don't want to force a
    single language. Also overridable via the `IMMY_WHISPER_PROMPT` env var.
    """

    clip_model: str | None = None
    # CLIP inference engine. "mlx" (default) embeds in-process on Apple
    # Silicon; "immich-ml" POSTs the preview to an Immich ML server (the NAS
    # path — no GPU/weights on the immy side). `immich_ml_url` is that
    # server's URL (e.g. http://n5:3003), required when clip_backend=immich-ml.
    clip_backend: str = "mlx"
    immich_ml_url: str | None = None
    whisper_model: str | None = None
    whisper_prompt: str | None = None
    # ASR inference engine. "mlx" (default) is the Apple-Silicon path; "whispercpp"
    # and "qwen-asr" are the NAS backends (Phase 2/5, see raw/IMMY-ON-N5.md).
    # `whisper_endpoint` points at a whisper.cpp / qwen-asr-serve HTTP server when
    # the chosen backend speaks HTTP rather than running in-process.
    whisper_backend: str = "mlx"
    whisper_endpoint: str | None = None
    # Captioner (Phase 3b). Any field None → captioner falls back to the
    # module-level defaults in `captions.py` (LM Studio on localhost,
    # Qwen2.5-VL-7B, no auth). Point at OpenAI/Anthropic/Gemini by
    # swapping `captioner_endpoint` + `captioner_model` +
    # `captioner_api_key_env`. The api-key field is an env-var *name*,
    # not the key itself — keeps secrets out of config.yml.
    captioner_endpoint: str | None = None
    captioner_model: str | None = None
    captioner_api_key_env: str | None = None
    captioner_prompt: str | None = None
    captioner_max_tokens: int | None = None
    # Provider-specific payload knobs merged verbatim into the request.
    # On the N5, set `extra_body: {reasoning_effort: none}` so Ollama's
    # gemma4 returns plain `content` instead of an empty string + `reasoning`.
    captioner_extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class Config:
    originals_root: Path | None
    immich: ImmichConfig | None
    pg: PgConfig | None
    media: MediaConfig | None
    ml: MLConfig | None
    notes_filename: str | None
    source: Path | None  # which file this came from, for error messages
    # Writable roots for NAS mode (:ro originals). Unset → Mac behavior
    # (state under `<trip>/.audit`, sidecars next to media). Placed last with
    # defaults so existing positional/keyword constructions stay valid.
    state_root: Path | None = None
    sidecars_root: Path | None = None


def _resolve_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    env = os.environ.get("IMMY_CONFIG")
    if env:
        return Path(env).expanduser()
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def load(path: Path | None = None) -> Config:
    resolved = _resolve_path(path)
    if resolved is None or not resolved.is_file():
        return Config(
            originals_root=None, immich=None, pg=None, media=None,
            ml=None, notes_filename=None, source=None,
        )
    data = yaml.safe_load(resolved.read_text()) or {}

    root_raw = data.get("originals_root")
    root = Path(root_raw).expanduser() if root_raw else None

    # Writable roots (NAS mode). YAML key, overridden by env var. Unset → None,
    # which keeps the Mac path under `<trip>/.audit` + sidecars-beside-media.
    sr_raw = os.environ.get("IMMY_STATE_ROOT") or data.get("state_root")
    state_root = Path(sr_raw).expanduser() if sr_raw else None
    sc_raw = os.environ.get("IMMY_SIDECARS_ROOT") or data.get("sidecars_root")
    sidecars_root = Path(sc_raw).expanduser() if sc_raw else None

    imm = data.get("immich") or {}
    immich = None
    if imm.get("url") and imm.get("api_key") and imm.get("library_id"):
        immich = ImmichConfig(
            url=str(imm["url"]).rstrip("/"),
            api_key=str(imm["api_key"]),
            library_id=str(imm["library_id"]),
        )

    pg_raw = data.get("pg") or {}
    pg = None
    if all(pg_raw.get(k) for k in ("host", "user", "password", "database")):
        pg = PgConfig(
            host=str(pg_raw["host"]),
            port=int(pg_raw.get("port", 5432)),
            user=str(pg_raw["user"]),
            password=str(pg_raw["password"]),
            database=str(pg_raw["database"]),
        )

    media_raw = data.get("media") or {}
    media = None
    if media_raw.get("host_root") and media_raw.get("container_root"):
        media = MediaConfig(
            host_root=str(media_raw["host_root"]).rstrip("/"),
            container_root=str(media_raw["container_root"]).rstrip("/"),
        )

    ml_raw = data.get("ml") or {}
    ml = None
    # Build MLConfig whenever an `[ml]` block exists at all — not only when
    # `clip_model` is set. A transcript/caption-only NAS deployment legitimately
    # omits clip_model (CLIP is delegated to Immich's ML server there), and it
    # must still pick up whisper_backend etc. instead of silently defaulting to
    # "mlx" and failing at inference time.
    if ml_raw:
        whisper = ml_raw.get("whisper_model")
        prompt = ml_raw.get("whisper_prompt")
        cap_raw = ml_raw.get("captioner") or {}
        ml = MLConfig(
            clip_model=str(ml_raw["clip_model"]) if ml_raw.get("clip_model") else None,
            clip_backend=str(ml_raw.get("clip_backend") or "mlx"),
            immich_ml_url=(
                str(ml_raw["immich_ml_url"])
                if ml_raw.get("immich_ml_url") else None
            ),
            whisper_model=str(whisper) if whisper else None,
            whisper_backend=str(ml_raw.get("whisper_backend") or "mlx"),
            whisper_endpoint=(
                str(ml_raw["whisper_endpoint"])
                if ml_raw.get("whisper_endpoint") else None
            ),
            whisper_prompt=str(prompt) if prompt else None,
            captioner_endpoint=(
                str(cap_raw["endpoint"]) if cap_raw.get("endpoint") else None
            ),
            captioner_model=(
                str(cap_raw["model"]) if cap_raw.get("model") else None
            ),
            captioner_api_key_env=(
                str(cap_raw["api_key_env"])
                if cap_raw.get("api_key_env") else None
            ),
            captioner_prompt=(
                str(cap_raw["prompt"]) if cap_raw.get("prompt") else None
            ),
            captioner_max_tokens=(
                int(cap_raw["max_tokens"])
                if cap_raw.get("max_tokens") else None
            ),
            captioner_extra=(
                dict(cap_raw["extra_body"])
                if isinstance(cap_raw.get("extra_body"), dict) else None
            ),
        )

    return Config(
        originals_root=root,
        immich=immich,
        pg=pg,
        media=media,
        ml=ml,
        notes_filename=data.get("notes_filename"),
        source=resolved,
        state_root=state_root,
        sidecars_root=sidecars_root,
    )
