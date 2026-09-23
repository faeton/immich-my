"""Content identity for the manifest (schema v4, todo/PHASE2-IDENTITY-DESIGN.md).

One rule underneath everything here: a staging file may be disposed of as a
duplicate only when **the library, right now, holds the exact same bytes** —
proven by a full sha256 of a real, non-symlinked file that is not the
staging file itself. `library_file` rows and `source_uid` matches are
evidence for *finding* a candidate; they never authorise a disposal alone.

- `hash_stable`       sha256 with a stat on both sides of the read, so a
                      file changing under the hash is detected, not indexed
- `library_eligible`  what counts as a library file (no dotfiles, temp names,
                      symlinks)
- `index_library`     walk library subtrees into `library_file`
- `library_match`     a library file holding these bytes, re-verified
- `resolve`           the per-asset decision `fingerprint_pending` acts on
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path

from . import manifest
from ..exif import MEDIA_EXTS

HASH_CHUNK = 4 * 1024 * 1024


class UnstableFile(RuntimeError):
    """The file changed (size, mtime, inode) while it was being hashed."""


def _key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def hash_stable(path: Path) -> tuple[str, os.stat_result]:
    """Full sha256 of `path`, plus the stat it is valid for.

    Refuses symlinks (a "library copy" that is a link into staging would
    vanish with the staging file it duplicates). Stats before and after the
    read; if anything moved, raises `UnstableFile` rather than recording
    old bytes under new metadata."""
    before = os.lstat(path)
    if stat_mod.S_ISLNK(before.st_mode):
        raise UnstableFile(f"{path} is a symlink")
    if not stat_mod.S_ISREG(before.st_mode):
        raise UnstableFile(f"{path} is not a regular file")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    after = os.lstat(path)
    if _key(before) != _key(after):
        raise UnstableFile(f"{path} changed while hashing")
    return h.hexdigest(), after


def no_symlinked_ancestor(path: Path) -> bool:
    """No component of `path` is a symlink: the path names the file directly,
    not through a link that could lead outside the library (or into
    staging)."""
    return os.path.realpath(path) == os.path.abspath(path)


def library_eligible(path: Path) -> bool:
    """Media that Immich would serve as an asset: a media extension and no
    hidden component anywhere in the path — which covers `_safe_copy`'s
    `.<name>.<pid>.partial`, triage's `.immy-new.<name>`, and files inside
    hidden directories however they were reached (walker, bootstrap,
    matching). Symlinks are rejected at hash time; a symlinked *directory*
    that makes a library path the staging file is caught by the same-inode
    check wherever a file would be disposed of."""
    return path.suffix.lower() in MEDIA_EXTS and not any(
        part.startswith(".") for part in path.parts
    )


# ------------------------------------------------------------ library index


@dataclass
class IndexResult:
    hashed: int = 0
    unchanged: int = 0
    pruned: int = 0
    skipped: int = 0   # unstable / symlink / unreadable


def index_library(
    conn: sqlite3.Connection,
    roots: list[Path],
    *,
    limit: int | None = None,
    batch_size: int = 200,
    progress=None,
) -> IndexResult:
    """Hash every eligible media file under `roots` into `library_file`.

    Resumable: a file whose (bytes, mtime_ns, inode) matches its row is not
    re-read. Rows under the walked roots whose file is gone (or no longer
    eligible) are pruned. Read-only on the files; commits per batch."""
    result = IndexResult()
    seen: set[str] = set()
    known = {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute("SELECT path, bytes, mtime_ns, inode FROM library_file")
    }
    pending = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if not library_eligible(path):
                    continue
                seen.add(str(path))
                try:
                    st = os.lstat(path)
                except OSError:
                    result.skipped += 1
                    continue
                if known.get(str(path)) == (st.st_size, st.st_mtime_ns, st.st_ino):
                    result.unchanged += 1
                    continue
                if limit is not None and result.hashed >= limit:
                    continue
                try:
                    sha, st = hash_stable(path)
                except (OSError, UnstableFile):
                    result.skipped += 1
                    continue
                manifest.upsert_library_file(conn, path, sha, st)
                result.hashed += 1
                pending += 1
                if pending >= batch_size:
                    conn.commit()
                    pending = 0
                    if progress:
                        progress(result.hashed)
    for root in roots:
        prefix = str(root).rstrip("/") + "/"
        for path in [p for p in known if p.startswith(prefix) and p not in seen]:
            conn.execute("DELETE FROM library_file WHERE path=?", (path,))
            result.pruned += 1
    conn.commit()
    return result


def library_match(
    conn: sqlite3.Connection, sha256: str, *, not_same_as: Path | None = None,
) -> Path | None:
    """A library file whose bytes are `sha256` right now, or None.

    Each candidate row is re-verified before it is returned: gone,
    symlinked or ineligible → the row is dropped; stat changed → re-hashed
    and the row corrected. A candidate that IS `not_same_as` (same inode —
    a hardlink or bind-mount view of the staging file) is never a match:
    quarantining one name of a file is not deduplication."""
    avoid = None
    if not_same_as is not None:
        try:
            s = os.stat(not_same_as)
            avoid = (s.st_dev, s.st_ino)
        except OSError:
            pass
    rows = conn.execute(
        "SELECT path, bytes, mtime_ns, inode FROM library_file WHERE sha256=? ORDER BY path",
        (sha256,),
    ).fetchall()
    for path_text, nbytes, mtime_ns, inode in rows:
        path = Path(path_text)
        try:
            st = os.lstat(path)
        except OSError:
            conn.execute("DELETE FROM library_file WHERE path=?", (path_text,))
            continue
        if (stat_mod.S_ISLNK(st.st_mode) or not library_eligible(path)
                or not no_symlinked_ancestor(path)):
            conn.execute("DELETE FROM library_file WHERE path=?", (path_text,))
            continue
        if avoid is not None and (st.st_dev, st.st_ino) == avoid:
            continue
        if (st.st_size, st.st_mtime_ns, st.st_ino) != (nbytes, mtime_ns, inode):
            try:
                fresh, st = hash_stable(path)
            except (OSError, UnstableFile):
                continue
            manifest.upsert_library_file(conn, path, fresh, st)
            if fresh != sha256:
                continue
        return path
    return None


def refresh_library_row(conn: sqlite3.Connection, path: Path) -> None:
    """Re-hash one indexed library file unconditionally (the stat key cannot
    be trusted — it is what just failed), or drop its row if it is gone or no
    longer eligible. Does not commit."""
    try:
        sha, st = hash_stable(path)
    except (OSError, UnstableFile):
        conn.execute("DELETE FROM library_file WHERE path=?", (str(path),))
        return
    if not library_eligible(path):
        conn.execute("DELETE FROM library_file WHERE path=?", (str(path),))
        return
    manifest.upsert_library_file(conn, path, sha, st)


def still_holds(library_path: Path, sha256: str, staging: Path) -> bool:
    """Disposal-time proof, both sides re-hashed in full: the library file
    still holds `sha256`, the staging file still holds `sha256`, and they are
    two different files. Anything unreadable or unstable is a no."""
    try:
        if os.path.samefile(library_path, staging):
            return False
        lib_sha, _ = hash_stable(library_path)
        src_sha, _ = hash_stable(staging)
    except (OSError, UnstableFile):
        return False
    return lib_sha == sha256 and src_sha == sha256


# --------------------------------------------------------------- resolution


@dataclass(frozen=True)
class Resolution:
    """What `fingerprint_pending` should do with one asset.

    kind: 'fingerprint' | 'alias' | 'error'."""
    kind: str
    sha256: str | None = None
    alias_path: str | None = None
    message: str | None = None
    stat: os.stat_result | None = None   # the stat `sha256` is valid for


STUB_PROBE = 64 * 1024


def _zero_head(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(STUB_PROBE)
    except OSError:
        return False
    return bool(head) and not head.strip(b"\0")


def stub_reason(nbytes: int, raw: dict, path: Path | None = None) -> str | None:
    """A file present in a batch that is not the real asset. Errors only —
    exiftool *warnings* are routine on real files. Header-level: `-fast2`
    cannot prove a video is complete; that is transport accounting's job.

    The zero-filled case is concrete, not hypothetical: the icloudpd tree on
    n5 (`staging/icloud`) is ~118k sparse placeholders — full apparent size,
    no data — left behind after promotion so icloudpd does not re-download.
    No real media file starts with 64 KiB of zeros."""
    if nbytes == 0:
        return "0 bytes"
    if path is not None and _zero_head(path):
        return "zero-filled placeholder (sparse stub)"
    for key in ("ExifTool:Error", "File:Error"):
        if raw.get(key):
            return f"{key}: {raw[key]}"[:200]
    # A media-named file whose content sniffs as text (an HTML error page, a
    # placeholder, a truncated-to-nothing download) — exiftool reports no
    # error for these, just a text MIME type.
    mime = str(raw.get("File:MIMEType") or "")
    if mime.startswith("text/"):
        return f"content is {mime}"
    return None


def resolve(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    source: str,
    path: Path,
    raw: dict,
    source_uid: str | None = None,
    component: str | None = None,
) -> Resolution:
    """Decide one registered asset's fate. Pure with respect to the asset row
    (the caller writes); may correct stale `library_file` rows as a side
    effect of verifying them.

    Order: stub → hash → library content (never for `originals`, which ARE
    the library) → UID revision check → normal fingerprint. A UID twin that
    is still in flight is NOT held: the cascade (identical bytes, same
    ContentIdentifier) handles it exactly as it did before v4, and holding
    it would need an ownership protocol for no gain in safety."""
    if source_uid and not component:
        return Resolution("error", message="source_uid without component")
    try:
        nbytes = os.lstat(path).st_size
    except OSError as exc:
        return Resolution("error", message=f"unreadable: {exc}")
    reason = stub_reason(nbytes, raw, path)
    if reason:
        return Resolution("error", message=f"stub: {reason}")
    try:
        sha, st = hash_stable(path)
    except UnstableFile as exc:
        return Resolution("error", message=f"unstable: {exc}")

    if source != "originals":
        hit = library_match(conn, sha, not_same_as=path)
        if hit is not None:
            return Resolution("alias", sha256=sha, alias_path=str(hit), stat=st)

    if source_uid:
        # A promoted/canonical twin with a KNOWN, DIFFERENT hash is a new
        # revision of one Photos asset. v1 does not model revisions: hold it
        # for a human rather than promote a second copy silently.
        prior = conn.execute(
            "SELECT id FROM asset WHERE source=? AND source_uid=? AND component=?"
            " AND id != ? AND status IN (?, ?) AND sha256 IS NOT NULL AND sha256 != ?"
            " ORDER BY id LIMIT 1",
            (source, source_uid, component, asset_id,
             manifest.PROMOTED, manifest.CANONICAL, sha),
        ).fetchone()
        if prior:
            return Resolution("error", sha256=sha, message=f"revision of #{prior[0]}")
    return Resolution("fingerprint", sha256=sha, stat=st)
