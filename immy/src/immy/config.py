"""User-level `immy` config loader.

Resolution order:
1. explicit path passed to `load(path=...)`
2. `$IMMY_CONFIG` env var
3. `~/.immy/config.yml`

Shape:

    originals_root: /mnt/incoming/originals-test
    notes_filename: TRIP.md          # optional; used by notes.py too
    immich:                          # optional; promote skips API if missing
      url: http://vv.tailnet:2283
      api_key: xxx
      library_id: <uuid>

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
class Config:
    originals_root: Path | None
    immich: ImmichConfig | None
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
        return Config(originals_root=None, immich=None, notes_filename=None, source=None)
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

    return Config(
        originals_root=root,
        immich=immich,
        notes_filename=data.get("notes_filename"),
        source=resolved,
    )
