"""Exact-match duplicate locator for any directory tree.

Given a local path and an Immich snapshot (see `snapshot.py`), walk the
tree and classify each file into one of four tiers:

    exact       filename + size match, SHA1 verified identical
    likely      filename + size match, hash unknown or not computed
    name-only   filename matches but size differs  (suspicious; could be
                re-export, edit, or unrelated file sharing a name)
    no-match    not present in the snapshot at all  (candidate for ingest)

We compute SHA1 only when `(filename, size)` already matched — reading
terabytes of unrelated files just to hash them is the wrong default. A
`--thorough` flag in the CLI enables hashing of *everything*, which
catches renames (file in snapshot under different name). A `--fast` flag
skips hashing entirely and everything above `no-match` lands as `likely`.

The walker honours gitignore-style patterns for noise files (.DS_Store,
Thumbs.db) and skips macOS bundles by default — stepping into
`Photos Library.photoslibrary` would take hours and isn't useful.
"""

from __future__ import annotations

import fnmatch
import hashlib
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from . import snapshot as snapshot_mod


# --- configuration defaults -----------------------------------------------

DEFAULT_IGNORE_GLOBS = (
    ".DS_Store",
    "._*",            # macOS resource forks on non-HFS volumes
    "Thumbs.db",
    "desktop.ini",
    "*.lrcat-*",      # Lightroom catalog backups
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
)

# Bundle directories we treat as opaque — walk refuses to descend.
DEFAULT_SKIP_BUNDLES = (
    "*.photoslibrary",
    "*.aplibrary",
    "*.app",
    "*.lrdata",
)

DEFAULT_MIN_SIZE = 0  # bytes; 0 means "keep everything"


# --- data model -----------------------------------------------------------


class Verdict(str, Enum):
    EXACT = "exact"
    LIKELY = "likely"
    NAME_ONLY = "name-only"
    NO_MATCH = "no-match"


@dataclass(frozen=True)
class ScanResult:
    """Outcome for a single local file."""

    path: Path
    size_bytes: int
    verdict: Verdict
    # Populated for exact/likely/name-only when the snapshot had any
    # matching filename. None for no-match.
    matched_asset_id: str | None = None
    matched_filename: str | None = None
    matched_size: int | None = None


@dataclass(frozen=True)
class ScanSummary:
    """Aggregated totals, ready to render."""

    files_scanned: int
    bytes_scanned: int
    by_verdict: dict[Verdict, list[ScanResult]] = field(default_factory=dict)

    def count(self, v: Verdict) -> int:
        return len(self.by_verdict.get(v, []))

    def bytes_of(self, v: Verdict) -> int:
        return sum(r.size_bytes for r in self.by_verdict.get(v, []))


class HashMode(str, Enum):
    """How aggressive the SHA1 computation should be."""

    # Hash only when (filename, size) matched — default. Cheap and covers
    # the "am I holding a duplicate?" question accurately.
    ON_MATCH = "on-match"

    # Hash nothing. Everything above no-match stays as `likely`. Fastest;
    # useful on slow spinning drives when you just want a rough overlap.
    FAST = "fast"

    # Hash every file. Finds renames (snapshot has the file under a
    # different name). Slow — reads the whole tree.
    THOROUGH = "thorough"


# --- walker ---------------------------------------------------------------


def _matches_any(name: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in globs)


def iter_candidate_files(
    root: Path,
    *,
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS,
    skip_bundles: tuple[str, ...] = DEFAULT_SKIP_BUNDLES,
    min_size: int = DEFAULT_MIN_SIZE,
    follow_symlinks: bool = False,
    into_bundles: bool = False,
) -> Iterator[Path]:
    """Yield files under `root` that pass the noise/bundle filters.

    Walks lazily so large trees don't blow up memory. Symlinks are ignored
    by default — following them on a typical backup disk gets you into
    time-machine snapshots and dead loops.
    """
    bundles = () if into_bundles else skip_bundles
    # Resolve once — walking a symlinked root itself is fine, we just
    # don't follow symlinks encountered during the walk.
    root = root if follow_symlinks else Path(root)
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        for p in entries:
            name = p.name
            if _matches_any(name, ignore_globs):
                continue
            # lstat avoids following symlinks to determine type.
            try:
                st = p.lstat()
            except (OSError, FileNotFoundError):
                continue
            import stat as _stat
            mode = st.st_mode
            if _stat.S_ISLNK(mode) and not follow_symlinks:
                continue
            if _stat.S_ISDIR(mode):
                if _matches_any(name, bundles):
                    continue
                stack.append(p)
                continue
            if not _stat.S_ISREG(mode):
                continue
            if st.st_size < min_size:
                continue
            yield p


# --- matching -------------------------------------------------------------


_HASH_CHUNK = 1024 * 1024  # 1 MiB — fits the L2 cache, big enough to amortise syscalls


def sha1_of(path: Path) -> bytes:
    """Stream-hash a file. Returns raw 20-byte SHA1 digest."""
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.digest()


def classify_one(
    path: Path,
    db: sqlite3.Connection,
    *,
    hash_mode: HashMode = HashMode.ON_MATCH,
) -> ScanResult:
    """Classify a single file against the snapshot.

    `db` is a read-only connection returned by `snapshot.open_for_read`.
    """
    try:
        size = path.stat().st_size
    except (OSError, FileNotFoundError):
        # File disappeared between walk and stat. Treat as no-match so the
        # run doesn't crash on a mutating tree.
        return ScanResult(path=path, size_bytes=0, verdict=Verdict.NO_MATCH)

    # Primary path: name + size lookup. Cheap index scan.
    matches = snapshot_mod.match_name_size(db, path.name, size)

    if matches:
        pick = matches[0]  # multiple is rare; any match is enough for verdict

        if hash_mode == HashMode.FAST:
            return ScanResult(
                path=path, size_bytes=size, verdict=Verdict.LIKELY,
                matched_asset_id=pick.asset_id,
                matched_filename=pick.filename,
                matched_size=pick.size_bytes,
            )

        # ON_MATCH and THOROUGH both hash here. We already have a name+size
        # match — verify byte identity.
        if pick.checksum is None:
            # Snapshot has no hash for this asset (pre-exif Immich state).
            # We can't confirm exact; downgrade to likely.
            return ScanResult(
                path=path, size_bytes=size, verdict=Verdict.LIKELY,
                matched_asset_id=pick.asset_id,
                matched_filename=pick.filename,
                matched_size=pick.size_bytes,
            )
        local = sha1_of(path)
        if local == pick.checksum:
            return ScanResult(
                path=path, size_bytes=size, verdict=Verdict.EXACT,
                matched_asset_id=pick.asset_id,
                matched_filename=pick.filename,
                matched_size=pick.size_bytes,
            )
        # Same name + size but different bytes — edge case (rolled-over
        # filename? collision?). Classify as name-only so it surfaces in
        # the report for manual review.
        return ScanResult(
            path=path, size_bytes=size, verdict=Verdict.NAME_ONLY,
            matched_asset_id=pick.asset_id,
            matched_filename=pick.filename,
            matched_size=pick.size_bytes,
        )

    # No name+size match. Name-only fallback: see if the filename exists in
    # the snapshot at a different size. Cheap: index on (filename, size) so
    # a name-only probe just selects by filename.
    cur = db.execute(
        "SELECT asset_id, filename, size_bytes FROM assets"
        " WHERE filename = ? LIMIT 1",
        (path.name,),
    )
    row = cur.fetchone()
    if row is not None:
        # Name matches, size differs. In THOROUGH mode, hash and try a
        # checksum lookup too — it might be the same bytes under a
        # different size encoding (unlikely) or actually renamed.
        if hash_mode == HashMode.THOROUGH:
            local = sha1_of(path)
            chk_matches = snapshot_mod.match_checksum(db, local)
            if chk_matches:
                # Renamed file: different name in snapshot, same bytes.
                pick = chk_matches[0]
                return ScanResult(
                    path=path, size_bytes=size, verdict=Verdict.EXACT,
                    matched_asset_id=pick.asset_id,
                    matched_filename=pick.filename,
                    matched_size=pick.size_bytes,
                )
        return ScanResult(
            path=path, size_bytes=size, verdict=Verdict.NAME_ONLY,
            matched_asset_id=row[0],
            matched_filename=row[1],
            matched_size=row[2],
        )

    # THOROUGH catches pure renames even without the name hint.
    if hash_mode == HashMode.THOROUGH:
        local = sha1_of(path)
        chk_matches = snapshot_mod.match_checksum(db, local)
        if chk_matches:
            pick = chk_matches[0]
            return ScanResult(
                path=path, size_bytes=size, verdict=Verdict.EXACT,
                matched_asset_id=pick.asset_id,
                matched_filename=pick.filename,
                matched_size=pick.size_bytes,
            )

    return ScanResult(path=path, size_bytes=size, verdict=Verdict.NO_MATCH)


def scan(
    root: Path,
    snapshot_path: Path,
    *,
    hash_mode: HashMode = HashMode.ON_MATCH,
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS,
    skip_bundles: tuple[str, ...] = DEFAULT_SKIP_BUNDLES,
    min_size: int = DEFAULT_MIN_SIZE,
    follow_symlinks: bool = False,
    into_bundles: bool = False,
    progress=None,
) -> ScanSummary:
    """Top-level scan. Walks `root`, classifies each file, aggregates.

    `progress`, if given, is called with `(path, result)` after every file —
    useful for CLI progress bars. Keep it lightweight; called in the hot
    loop.
    """
    db = snapshot_mod.open_for_read(snapshot_path)
    try:
        files_scanned = 0
        bytes_scanned = 0
        by_verdict: dict[Verdict, list[ScanResult]] = {v: [] for v in Verdict}
        for path in iter_candidate_files(
            root,
            ignore_globs=ignore_globs,
            skip_bundles=skip_bundles,
            min_size=min_size,
            follow_symlinks=follow_symlinks,
            into_bundles=into_bundles,
        ):
            result = classify_one(path, db, hash_mode=hash_mode)
            files_scanned += 1
            bytes_scanned += result.size_bytes
            by_verdict[result.verdict].append(result)
            if progress is not None:
                progress(path, result)
        return ScanSummary(
            files_scanned=files_scanned,
            bytes_scanned=bytes_scanned,
            by_verdict=by_verdict,
        )
    finally:
        db.close()


# --- rendering ------------------------------------------------------------


def _human_bytes(n: int) -> str:
    """'4,723,452' → '4.5 GB'. Enough precision for a summary line."""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


def render_markdown(summary: ScanSummary, root: Path) -> str:
    """Render the full Markdown report — summary + per-tier tables."""
    lines: list[str] = []
    lines.append(f"# Duplicate scan report")
    lines.append("")
    lines.append(f"- Root: `{root}`")
    lines.append(f"- Files scanned: {summary.files_scanned:,}")
    lines.append(f"- Bytes scanned: {_human_bytes(summary.bytes_scanned)}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Verdict | Count | Bytes |")
    lines.append("|---|---:|---:|")
    # Stable tier order: exact → likely → name-only → no-match.
    for v in (Verdict.EXACT, Verdict.LIKELY, Verdict.NAME_ONLY, Verdict.NO_MATCH):
        lines.append(
            f"| {v.value} | {summary.count(v):,} |"
            f" {_human_bytes(summary.bytes_of(v))} |",
        )
    lines.append("")
    for v in (Verdict.EXACT, Verdict.LIKELY, Verdict.NAME_ONLY, Verdict.NO_MATCH):
        rows = summary.by_verdict.get(v, [])
        if not rows:
            continue
        lines.append(f"## {v.value} ({len(rows)})")
        lines.append("")
        if v == Verdict.NO_MATCH:
            lines.append("| Path | Size |")
            lines.append("|---|---:|")
            for r in rows:
                lines.append(f"| `{r.path}` | {_human_bytes(r.size_bytes)} |")
        else:
            lines.append("| Path | Size | Matched asset | Matched name |")
            lines.append("|---|---:|---|---|")
            for r in rows:
                lines.append(
                    f"| `{r.path}` | {_human_bytes(r.size_bytes)} |"
                    f" `{r.matched_asset_id or ''}` | `{r.matched_filename or ''}` |",
                )
        lines.append("")
    return "\n".join(lines)


def to_json_rows(summary: ScanSummary) -> list[dict]:
    """Flat list, one dict per scanned file. Stable shape for downstream."""
    out: list[dict] = []
    for v in Verdict:
        for r in summary.by_verdict.get(v, []):
            out.append({
                "path": str(r.path),
                "size_bytes": r.size_bytes,
                "verdict": r.verdict.value,
                "matched_asset_id": r.matched_asset_id,
                "matched_filename": r.matched_filename,
                "matched_size": r.matched_size,
            })
    return out
