"""Review decisions: <work>/_cull/decisions.json, {item id: {keep, trip, tags,
note, fp, updated}}. A decision remembers the fingerprint it was made on; if
the file changes under the same path the decision shows as `changed` and stage
refuses the item until it is reviewed again."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from .core.jsonio import now_iso, read_json, write_json_atomic

KEEP_STATES = ("unreviewed", "keep", "drop")
DEFAULT = {"keep": "unreviewed", "trip": None, "tags": [], "note": "", "fp": None, "updated": None}
TRIP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}(-[0-9]{2})?(-[a-z0-9]+)+$")
PATCH_KEYS = frozenset({"keep", "trip", "tags", "add_tags", "note"})
_LOCK = threading.Lock()


def _path(work: Path) -> Path:
    return Path(work) / "_cull" / "decisions.json"


def load(work: Path) -> dict[str, dict]:
    return read_json(_path(work), {}) or {}


def effective(item: dict, d: dict | None) -> dict:
    out = {**DEFAULT, **(d or {})}
    out["tags"] = list(out["tags"])
    out["changed"] = bool(out["keep"] != "unreviewed" and out["fp"] and out["fp"] != item.get("fp"))
    return out


def list_trips(trips: Path | None) -> list[str]:
    if trips is None or not Path(trips).is_dir():
        return []
    return sorted(p.name for p in Path(trips).iterdir() if p.is_dir() and not p.name.startswith("."))


def _tags(v, key: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        raise ValueError(f"{key} must be a list of strings")
    return [t.strip()[:64] for t in v if t.strip()]


def _merge_tags(old: list[str], new: list[str]) -> list[str]:
    seen = {t.lower() for t in old}
    out = list(old)
    for t in new:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _check(patch: dict, existing_trips: set[str]) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch must be a non-empty object")
    extra = set(patch) - PATCH_KEYS
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(sorted(extra))}")
    out = {}
    if "keep" in patch:
        if patch["keep"] not in KEEP_STATES:
            raise ValueError(f"keep must be one of {', '.join(KEEP_STATES)}")
        out["keep"] = patch["keep"]
    if "trip" in patch:
        t = patch["trip"]
        if t is not None:
            if not isinstance(t, str) or not (t in existing_trips or TRIP_RE.fullmatch(t)):
                raise ValueError("trip must be an existing trip folder or look like YYYY-MM-place")
        out["trip"] = t
    if "tags" in patch:
        out["tags"] = _tags(patch["tags"], "tags")
    if "add_tags" in patch:
        out["add_tags"] = _tags(patch["add_tags"], "add_tags")
    if "note" in patch:
        if not isinstance(patch["note"], str):
            raise ValueError("note must be a string")
        out["note"] = patch["note"].strip()[:2000]
    return out


def apply(work: Path, items_by_id: dict, ids: list[str], patch: dict, existing_trips: set[str]) -> dict[str, dict]:
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValueError("ids must be a non-empty list")
    unknown = [i for i in ids if i not in items_by_id]
    if unknown:
        raise ValueError(f"unknown item id: {unknown[0]}")
    p = _check(patch, existing_trips)
    with _LOCK:
        all_d = load(work)
        for i in ids:
            d = {**DEFAULT, **all_d.get(i, {})}
            d["tags"] = list(d["tags"])
            if "keep" in p:
                d["keep"] = p["keep"]
                d["fp"] = items_by_id[i].get("fp") if p["keep"] != "unreviewed" else None
            if "trip" in p:
                d["trip"] = p["trip"]
            if "tags" in p:
                d["tags"] = _merge_tags([], p["tags"])
            if "add_tags" in p:
                d["tags"] = _merge_tags(d["tags"], p["add_tags"])
            if "note" in p:
                d["note"] = p["note"]
            d["updated"] = now_iso()
            all_d[i] = d
        write_json_atomic(_path(work), all_d)
    return {i: effective(items_by_id[i], all_d[i]) for i in ids}
