"""Verdict executor — the file-touching half of triage. v1: `compress`.

Everything before this writes only data; this module is where footage
actually changes. Per compress-verdict clip:

    encode → verify → same-path swap (original → quarantine) → stamp
    `applied_at` → (end of run) one Immich library rescan.

Same-path is load-bearing: Immich keys external-library assets by
`originalPath`, so an in-place swap keeps asset identity — albums,
favorites, shares all survive; the rescan just re-reads the changed file.
Which is also why the container NEVER changes:

    .mp4 → SVT-AV1 10-bit in .mp4
    .mov → x265 HEVC 10-bit in .mov   (AV1-in-mov is not a thing)

Crash-safety: the new file lands beside the original as `.<name>.immy-new`,
the original moves to quarantine, then a same-directory atomic rename
finishes the swap. A crash between those two steps leaves an orphan
`.immy-new` next to a missing original — `heal()` runs first on every
invocation and completes exactly that rename.

Deliberate loss: only the primary video + audio streams are kept. DJI's
embedded data tracks (telemetry) are dropped — immy already extracts them
to durable .srt/.gpx sidecars at ingest, and the mp4 muxer chokes on them.
EXIF/GPS/creation time survive via -map_metadata + an exiftool pass, and
the file's mtime is copied from the original.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .engine import map_path, trip_of

# Duration may drift a hair on remux; more than this means a broken encode.
DURATION_TOLERANCE_S = 0.75
# If the encode doesn't beat the original by at least this factor, keep the
# original — re-encoding for a 3% win is pure quality loss.
MIN_GAIN = 0.90

AV1_CRF = 30
HEVC_CRF = 22


def encode_cmd(src: Path, dst: Path, threads: int) -> list[str]:
    ext = src.suffix.lower()
    if ext == ".mov":
        codec = [
            "-c:v", "libx265", "-crf", str(HEVC_CRF), "-preset", "medium",
            "-x265-params", f"pools={threads}:log-level=error",
            "-tag:v", "hvc1",
        ]
    else:
        codec = [
            "-c:v", "libsvtav1", "-crf", str(AV1_CRF), "-preset", "6",
            "-svtav1-params", f"tune=0:lp={threads}",
        ]
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        *codec, "-pix_fmt", "yuv420p10le",
        "-c:a", "copy",
        "-map_metadata", "0", "-movflags", "+faststart",
        "-y", str(dst),
    ]


def ffprobe_duration(path: Path) -> float | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def default_encode(src: Path, dst: Path, threads: int) -> None:
    proc = subprocess.run(encode_cmd(src, dst, threads), capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg failed: {(proc.stderr or '').strip()[-300:]}")
    subprocess.run(  # best-effort EXIF/GPS carry-over; metadata mostly maps already
        ["exiftool", "-overwrite_original", "-TagsFromFile", str(src),
         "-gps:all", "-exif:all", "-quicktime:CreateDate", str(dst)],
        capture_output=True,
    )


NEW_PREFIX = ".immy-new."


def heal(originals_fs: Path, log: Callable[[str], None]) -> int:
    """Finish any swap a crash interrupted: an orphan `.immy-new.<name>`
    whose target is missing gets renamed into place."""
    healed = 0
    for orphan in originals_fs.rglob(f"{NEW_PREFIX}*"):
        target = orphan.with_name(orphan.name[len(NEW_PREFIX):])
        if not target.exists():
            orphan.rename(target)
            log(f"healed interrupted swap: {target.name}")
            healed += 1
        else:
            orphan.unlink()  # swap completed; leftover staging copy
            log(f"removed stale staging copy for {target.name}")
    return healed


def _log_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS exec_log ("
        " asset_id INTEGER, action TEXT, status TEXT,"
        " in_bytes INTEGER, out_bytes INTEGER, detail TEXT, at TEXT)"
    )


@dataclass
class CompressResult:
    processed: int = 0
    swapped: int = 0
    no_gain: int = 0
    failed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finalize_swap(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    mpath: str,
    src: Path,
    new_file: Path,
    quarantine_root: Path,
    root: str,
    action: str,
) -> int:
    """The shared safe-swap tail (see module docstring): stage the verified
    new file beside the original, quarantine the original, atomic rename,
    carry owner/mode/mtime, stamp + journal. Consumes `new_file`. Returns
    the swapped-in size."""
    st = src.stat()
    out_bytes = new_file.stat().st_size
    os.utime(new_file, (st.st_atime, st.st_mtime))
    staged = src.with_name(NEW_PREFIX + src.name)
    shutil.move(str(new_file), staged)
    trip = trip_of(mpath, root) or "_untripped"
    qdst = quarantine_root / trip / src.name
    qdst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), qdst)
    staged.rename(src)
    try:
        os.chown(src, st.st_uid, st.st_gid)
        os.chmod(src, st.st_mode)
    except OSError:
        pass
    conn.execute(
        "UPDATE triage SET applied_at=? WHERE asset_id=?", (_now(), asset_id)
    )
    # The swap deliberately carries the original's mtime onto the new file,
    # so a (bytes, mtime) freshness check alone could miss it: drop the
    # content hash and the library index entry for this path explicitly.
    conn.execute(
        "UPDATE asset SET bytes=?, mtime=?, sha256=NULL WHERE id=?",
        (out_bytes, src.stat().st_mtime, asset_id),
    )
    conn.execute("DELETE FROM library_file WHERE path=?", (mpath,))
    conn.execute(
        f"INSERT INTO exec_log VALUES (?, '{action}', 'swapped', ?, ?, ?, ?)",
        (asset_id, st.st_size, out_bytes, str(qdst), _now()),
    )
    conn.commit()
    return out_bytes


def apply_ingest(
    conn: sqlite3.Connection,
    *,
    returns_root: Path,
    root: str = "/originals",
    fs_root: str | None = None,
    quarantine_root: Path = Path("/quarantine/compress-originals"),
    dry_run: bool = True,
    duration_fn: Callable[[Path], float | None] = ffprobe_duration,
    progress: Callable[[int, int, str], None] = lambda i, n, name: None,
    log: Callable[[str], None] = lambda s: None,
) -> CompressResult:
    """Ingest encodes produced by an external worker (the GPU laptop):
    every file under `returns_root` whose trip-relative path matches a
    pending compress verdict goes through the SAME verification and swap
    as a local encode — duration against the original, size gain, then
    `_finalize_swap`. The worker is untrusted by design: unmatched files
    are left in place and logged, `.part`/dot files (in-progress writes)
    are skipped, and a bad encode just fails verification and stays
    pending for the local CPU path.

    A successful swap CONSUMES the returned file; a no-gain result stamps
    the verdict and deletes the return. Run repeatedly while the worker
    is still producing — each pass picks up whatever has landed."""
    fs_root = fs_root or root
    _log_table(conn)
    if not dry_run:
        heal(Path(fs_root), log)

    pending = {
        p: (asset_id, in_bytes) for asset_id, p, in_bytes in conn.execute(
            "SELECT t.asset_id, a.path, a.bytes FROM triage t"
            " JOIN asset a ON a.id = t.asset_id"
            " WHERE t.verdict='compress' AND t.applied_at IS NULL"
        )
    }
    files = [
        f for f in sorted(returns_root.rglob("*"))
        if f.is_file() and not f.name.startswith(".")
        and f.suffix.lower() not in (".part", ".bad", ".jsonl", ".json", ".md", ".txt", ".log")
    ]
    result = CompressResult()
    for i, ret in enumerate(files):
        rel = ret.relative_to(returns_root).as_posix()
        mpath = f"{root.rstrip('/')}/{rel}"
        progress(i + 1, len(files), rel)
        entry = pending.get(mpath)
        if entry is None:
            conn.execute(
                "INSERT INTO exec_log VALUES (NULL, 'ingest', 'unmatched', "
                " NULL, ?, ?, ?)",
                (ret.stat().st_size, rel, _now()),
            )
            conn.commit()
            log(f"unmatched (left in place): {rel}")
            continue
        asset_id, in_bytes = entry
        result.processed += 1
        if dry_run:
            result.bytes_in += in_bytes or 0
            continue
        src = map_path(mpath, root, fs_root)
        try:
            if not src.exists():
                raise RuntimeError(f"original missing: {src}")
            src_dur, out_dur = duration_fn(src), duration_fn(ret)
            if src_dur and (out_dur is None or abs(out_dur - src_dur) > DURATION_TOLERANCE_S):
                raise RuntimeError(f"duration mismatch {src_dur} → {out_dur}")
            out_bytes = ret.stat().st_size
            if out_bytes == 0:
                raise RuntimeError("empty return")
            if out_bytes >= (in_bytes or 0) * MIN_GAIN:
                conn.execute(
                    "UPDATE triage SET applied_at=?, reason=COALESCE(reason,'') "
                    " || ' [no-gain: kept original]' WHERE asset_id=?",
                    (_now(), asset_id),
                )
                conn.execute(
                    "INSERT INTO exec_log VALUES (?, 'ingest', 'no-gain', ?, ?, NULL, ?)",
                    (asset_id, in_bytes, out_bytes, _now()),
                )
                conn.commit()
                ret.unlink()
                result.no_gain += 1
                continue
            _finalize_swap(
                conn, asset_id=asset_id, mpath=mpath, src=src, new_file=ret,
                quarantine_root=quarantine_root, root=root, action="ingest",
            )
            result.swapped += 1
            result.bytes_in += in_bytes or 0
            result.bytes_out += out_bytes
            log(f"{rel}: {(in_bytes or 0) / 1e9:.2f}G → {out_bytes / 1e9:.2f}G")
            pending.pop(mpath, None)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            result.failed += 1
            conn.execute(
                "INSERT INTO exec_log VALUES (?, 'ingest', 'failed', ?, NULL, ?, ?)",
                (asset_id, in_bytes, str(e)[:300], _now()),
            )
            conn.commit()
            log(f"FAIL {rel}: {e}")
            try:
                # park the bad return so the next pass doesn't re-fail it;
                # the verdict stays pending for the local CPU path
                ret.rename(ret.with_name(ret.name + ".bad"))
            except OSError:
                pass
    return result


def apply_compress(
    conn: sqlite3.Connection,
    *,
    root: str = "/originals",
    fs_root: str | None = None,
    quarantine_root: Path = Path("/quarantine/compress-originals"),
    scratch: Path = Path("/scratch/triage-exec"),
    threads: int = 8,
    limit: int | None = None,
    smallest_first: bool = False,
    dry_run: bool = True,
    encode_fn: Callable[[Path, Path, int], None] = default_encode,
    duration_fn: Callable[[Path], float | None] = ffprobe_duration,
    progress: Callable[[int, int, str], None] = lambda i, n, name: None,
    log: Callable[[str], None] = lambda s: None,
) -> CompressResult:
    fs_root = fs_root or root
    _log_table(conn)
    if not dry_run:
        heal(Path(fs_root), log)

    rows = conn.execute(
        "SELECT t.asset_id, a.path, a.bytes FROM triage t"
        " JOIN asset a ON a.id = t.asset_id"
        " WHERE t.verdict='compress' AND t.applied_at IS NULL"
        "   AND LOWER(a.path) NOT LIKE '%.insv'"
        # biggest savings first — ^C keeps the wins; smallest-first is for
        # smoke runs where you want a fast end-to-end proof
        f" ORDER BY a.bytes {'ASC' if smallest_first else 'DESC'}"
    ).fetchall()
    if limit:
        rows = rows[:limit]

    result = CompressResult()
    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

    for i, (asset_id, mpath, in_bytes) in enumerate(rows):
        src = map_path(mpath, root, fs_root)
        progress(i + 1, len(rows), src.name)
        result.processed += 1
        if dry_run:
            result.bytes_in += in_bytes or 0
            continue
        if not src.exists():
            result.failed += 1
            conn.execute(
                "INSERT INTO exec_log VALUES (?, 'compress', 'missing', ?, NULL, ?, ?)",
                (asset_id, in_bytes, str(src), now()),
            )
            conn.commit()
            continue
        tmp = scratch / f"{asset_id}{src.suffix.lower()}"
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.unlink(missing_ok=True)
            encode_fn(src, tmp, threads)

            src_dur, out_dur = duration_fn(src), duration_fn(tmp)
            if src_dur and (out_dur is None or abs(out_dur - src_dur) > DURATION_TOLERANCE_S):
                raise RuntimeError(f"duration mismatch {src_dur} → {out_dur}")
            out_bytes = tmp.stat().st_size
            if out_bytes >= (in_bytes or 0) * MIN_GAIN:
                # Not worth a generation of quality loss — keep the original
                # and stamp applied so it never retries.
                conn.execute(
                    "UPDATE triage SET applied_at=?, reason=COALESCE(reason,'') "
                    " || ' [no-gain: kept original]' WHERE asset_id=?",
                    (now(), asset_id),
                )
                conn.execute(
                    "INSERT INTO exec_log VALUES (?, 'compress', 'no-gain', ?, ?, NULL, ?)",
                    (asset_id, in_bytes, out_bytes, now()),
                )
                conn.commit()
                result.no_gain += 1
                continue

            _finalize_swap(
                conn, asset_id=asset_id, mpath=mpath, src=src, new_file=tmp,
                quarantine_root=quarantine_root, root=root, action="compress",
            )
            result.swapped += 1
            result.bytes_in += in_bytes or 0
            result.bytes_out += out_bytes
            log(f"{src.name}: {in_bytes / 1e9:.2f}G → {out_bytes / 1e9:.2f}G")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            result.failed += 1
            conn.execute(
                "INSERT INTO exec_log VALUES (?, 'compress', 'failed', ?, NULL, ?, ?)",
                (asset_id, in_bytes, str(e)[:300], now()),
            )
            conn.commit()
            log(f"FAIL {src.name}: {e}")
        finally:
            tmp.unlink(missing_ok=True)
    return result
