"""User-level `immy` config loader.

Resolution order:
1. explicit path passed to `load(path=...)`
2. `$IMMY_CONFIG` env var
3. `~/.immy/config.yml`

Shape:

    originals_root: /mnt/incoming/originals-test
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

Missing config file is not an error for `audit`; `promote` checks what it
needs and raises a clear message if `originals_root` is absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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
    """

    clip_model: str


@dataclass(frozen=True)
class Config:
    originals_root: Path | None
    immich: ImmichConfig | None
    pg: PgConfig | None
    media: MediaConfig | None
    ml: MLConfig | None
    notes_filename: str | None
    source: Path | None  # which file this came from, for error messages


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
    if ml_raw.get("clip_model"):
        ml = MLConfig(clip_model=str(ml_raw["clip_model"]))

    return Config(
        originals_root=root,
        immich=immich,
        pg=pg,
        media=media,
        ml=ml,
        notes_filename=data.get("notes_filename"),
        source=resolved,
    )
