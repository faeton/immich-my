"""Review items: one shot = one primary file plus everything that belongs to it.

Files in one directory sharing a stem (case-insensitive) belong together. A
`name.ext.xmp` sidecar belongs to `name.ext`. Same-stem stills (DNG+JPG, or a
HEIC/JPG + MOV live photo) are one shot; a video and a raw still that happen
to share a stem are two items. Companions without any media are orphans.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mts"})
RAW_EXTS = frozenset({".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".orf", ".rw2"})
STILL_EXTS = frozenset({".jpg", ".jpeg", ".heic", ".heif", ".png"}) | RAW_EXTS
COMPANION_EXTS = frozenset({".srt", ".lrf", ".thm", ".xmp", ".aae"})
MEDIA_EXTS = VIDEO_EXTS | STILL_EXTS
SKIP_DIRS = frozenset({"_cull", "_review", "_render"})
LIVE_STILLS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})


@dataclass
class Group:
    src: str
    files: list[str]
    kind: str

    @property
    def id(self) -> str:
        return f"{self.src}/{self.files[0]}"


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def register_sources(existing: dict[str, str], srcs: list[Path]) -> dict[str, str]:
    """Name each new source by its basename (`-2`, `-3` when taken); known paths keep their name."""
    out = dict(existing)
    by_path = {v: k for k, v in out.items()}
    for s in srcs:
        p = str(Path(s).expanduser().resolve())
        if p in by_path:
            continue
        base = Path(p).name
        name, i = base, 2
        while name in out:
            name, i = f"{base}-{i}", i + 1
        out[name] = p
        by_path[p] = name
    return out


def _split(names: list[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    videos = sorted(n for n in names if _ext(n) in VIDEO_EXTS)
    stills = sorted(n for n in names if _ext(n) in STILL_EXTS)
    comps = sorted(n for n in names if _ext(n) in COMPANION_EXTS)
    if not videos and not stills:
        return [], comps
    plain = [s for s in stills if _ext(s) not in RAW_EXTS]
    if (len(videos) == 1 and _ext(videos[0]) == ".mov" and len(stills) == 1 and plain
            and _ext(plain[0]) in LIVE_STILLS):
        return [("photo", [plain[0], videos[0], *comps])], []

    def own_xmp(master: str) -> list[str]:
        return [c for c in comps if c.lower() == (master + ".xmp").lower()]

    out: list[tuple[str, list[str]]] = []
    if stills:
        prim = plain[0] if plain else stills[0]
        rest = [s for s in stills if s != prim]
        out.append(("photo", [prim, *rest, *[x for s in [prim, *rest] for x in own_xmp(s)]]))
    for v in videos:
        out.append(("video", [v, *own_xmp(v)]))
    taken = {n for _, fs in out for n in fs}
    left = [c for c in comps if c not in taken]
    if left:
        target = next((fs for kind, fs in out if kind == "video"), out[0][1])
        target.extend(left)
    return out, []


def walk(src_name: str, root: Path) -> tuple[list[Group], list[str]]:
    root = Path(root)
    groups: list[Group] = []
    orphans: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        d = Path(dirpath)
        by_key: dict[str, list[str]] = {}
        for n in sorted(filenames):
            if n.startswith("._") or n == ".DS_Store":
                continue
            p = d / n
            if p.is_symlink() or not p.is_file():
                continue
            stem, ext = os.path.splitext(n)
            ext = ext.lower()
            if ext == ".xmp" and _ext(stem) in MEDIA_EXTS:
                stem = os.path.splitext(stem)[0]
            elif ext not in MEDIA_EXTS | COMPANION_EXTS:
                continue
            by_key.setdefault(stem.lower(), []).append(n)
        rel = d.relative_to(root)
        for key in sorted(by_key):
            found, orph = _split(by_key[key])
            for kind, files in found:
                groups.append(Group(src_name, [(rel / f).as_posix() for f in files], kind))
            orphans += [(rel / f).as_posix() for f in orph]
    return groups, orphans


def fingerprint(root: Path, files: list[str]) -> str:
    h = hashlib.sha1()
    for f in files:
        st = (Path(root) / f).stat()
        h.update(f"{f}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]
