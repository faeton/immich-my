"""Folder notes file: TRIP.md → IMMY.md → README.md.

First audit auto-creates README.md with a trip-identity front-matter
block if none of the three exist. Existing notes are never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .exif import ExifRow


NOTES_ORDER = ("TRIP.md", "IMMY.md", "README.md")
DEFAULT_WRITE = "README.md"


def resolve(folder: Path) -> Path | None:
    for name in NOTES_ORDER:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (yaml_block, body). yaml_block empty string if no front-matter."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    yaml_block = text[3:end].lstrip("\n")
    after = text[end + 4:]
    if after.startswith("\n"):
        after = after[1:]
    return yaml_block, after


def parse_frontmatter(notes: Path) -> dict:
    """Return the YAML front-matter dict. Empty if file has none."""
    yaml_block, _ = _split_frontmatter(notes.read_text(errors="replace"))
    if not yaml_block:
        return {}
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def update_frontmatter(notes: Path, updates: dict) -> None:
    """Deep-merge `updates` into the notes front-matter and rewrite the file.
    Preserves the body verbatim. Comments in the YAML block are lost — the
    block is re-emitted from the parsed dict."""
    text = notes.read_text(errors="replace")
    yaml_block, body = _split_frontmatter(text)
    data: dict = {}
    if yaml_block:
        try:
            loaded = yaml.safe_load(yaml_block)
            if isinstance(loaded, dict):
                data = loaded
        except yaml.YAMLError:
            pass
    _deep_merge(data, updates)
    new_text = "---\n" + yaml.safe_dump(data, sort_keys=False).rstrip() + "\n---\n" + body
    notes.write_text(new_text)


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def target_for_write(folder: Path) -> Path:
    existing = resolve(folder)
    return existing if existing is not None else folder / DEFAULT_WRITE


def notes_body(notes: Path) -> str:
    """Return the body text below the front-matter, with a leading `# Title`
    heading stripped if it matches the trip identity (so the Immich album
    title isn't also repeated inside the description).

    Also skips the scaffold hint block `immy audit` writes on first run —
    those starting lines like *"Scaffold by `immy audit`..."* are noise in
    an album description and disappear once the user edits the notes.
    """
    text = notes.read_text(errors="replace")
    _, body = _split_frontmatter(text)
    lines = body.splitlines()
    # Drop leading blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    # Drop a leading `# Heading` — the album already has the trip name.
    if lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    # Drop the scaffold hint block (it's wrapped in italics and always
    # mentions `immy audit`). Match paragraph-by-paragraph.
    paragraphs = _split_paragraphs(lines)
    paragraphs = [
        p for p in paragraphs
        if not _is_scaffold_hint(p)
    ]
    return "\n\n".join(paragraphs).strip()


def _split_paragraphs(lines: list[str]) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if ln.strip():
            buf.append(ln)
        elif buf:
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out


def _is_scaffold_hint(paragraph: str) -> bool:
    stripped = paragraph.strip()
    if not stripped.startswith("_") or not stripped.rstrip().endswith("_"):
        return False
    return "immy audit" in stripped


@dataclass
class TripIdentity:
    name: str
    dates: list[str]
    cameras: list[str]
    filename_prefixes: list[str]


_PREFIX_RE = None


def _prefix(path: Path) -> str | None:
    name = path.stem
    if "_" not in name:
        return None
    head = name.split("_", 1)[0].upper()
    # filter noise: pure-digit heads aren't useful as a Source/ tag.
    if head.isdigit() or len(head) > 6:
        return None
    return head


def detect_identity(folder: Path, rows: list[ExifRow]) -> TripIdentity:
    dates: set[str] = set()
    cameras: set[str] = set()
    prefixes: set[str] = set()
    for r in rows:
        for k in ("EXIF:DateTimeOriginal", "QuickTime:CreateDate"):
            v = r.get(k)
            if isinstance(v, str) and len(v) >= 10:
                dates.add(v[:10].replace(":", "-"))
                break
        make = r.get("EXIF:Make", "QuickTime:Make") or ""
        model = r.get("EXIF:Model", "QuickTime:Model") or ""
        cam = f"{make} {model}".strip()
        if cam:
            cameras.add(cam)
        p = _prefix(r.path)
        if p:
            prefixes.add(p)
    return TripIdentity(
        name=folder.name,
        dates=sorted(dates),
        cameras=sorted(cameras),
        filename_prefixes=sorted(prefixes),
    )


def suggested_tags(identity: TripIdentity) -> list[str]:
    tags: list[str] = [f"Events/{identity.name}"]
    for cam in identity.cameras:
        tags.append(f"Gear/Camera/{cam}")
    for prefix in identity.filename_prefixes:
        tags.append(f"Source/{prefix}")
    return tags


def ensure_notes(folder: Path, rows: list[ExifRow]) -> Path | None:
    """Create a notes file with trip identity if none exists. Returns path
    if created, else None."""
    if resolve(folder) is not None:
        return None
    identity = detect_identity(folder, rows)
    front = {
        "trip": identity.name,
        "dates": identity.dates,
        "cameras": identity.cameras,
        "location": {"name": None, "coords": None},
        "timezone": None,
        "tags": suggested_tags(identity),
    }
    body = [
        "---",
        yaml.safe_dump(front, sort_keys=False).rstrip(),
        "---",
        "",
        f"# {identity.name}",
        "",
        "_Scaffold by `immy audit`. Fill `location` (either `name:` or",
        "`coords: [lat, lon]`). Edit `tags:` to taste. Front-matter above",
        "drives XMP writes on the next `--write`._",
        "",
    ]
    target = folder / DEFAULT_WRITE
    target.write_text("\n".join(body))
    return target
