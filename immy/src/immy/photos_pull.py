"""Export Photos.app assets by UUID and deliver them to n5 as immutable batches.

Phase 4 of the Photos bridge (`todo/PHOTOS-BRIDGE-REVIEW.md`). Input is the
UUID list from `immy photos diff`; output is one directory per batch under
n5's `staging/photos/ready/`, which `immy dedup register photos` walks.

    osxphotos export <local>/<batch> --uuid-from-file …   (Photos.app downloads
          │                                                iCloud-only originals
          ▼                                                via PhotoKit)
    completeness check per UUID
          │
    Photos.app AppleScript `export … with using originals`, per incomplete
          │   UUID (fetches iCloud-only originals PhotoKit reports "missing")
          ▼
    completeness check again (drop what's still half-exported)
          │
    rsync → n5:<root>/.staging/<batch>/  → verify listing → mv → <root>/ready/<batch>/

Two guarantees the review asked for (P3.1 / P3.2), both carried by a ledger
keyed on UUID rather than on an export watermark or osxphotos' own export DB:

- **A failed transfer is retried.** A batch stays `exported` until n5's
  `ready/` holds it with a matching listing; the next run re-sends it before
  exporting anything new. Every batch is a fresh export directory, so
  osxphotos' "skipped, already exported" state can never hide a file.
- **Half an asset is never delivered.** A UUID counts as exported only if it
  has a media file with no error — and, for a Live Photo, the paired video
  too. Otherwise its files are removed from the batch and the UUID goes back
  to `failed`, eligible again next run (until `MAX_ATTEMPTS`).

Photos.app fallback: on the 2026-10 backlog, 24 edited Live Photos had their
original video only in iCloud. osxphotos' PhotoKit path reported it missing
without downloading it; Photos.app's own AppleScript originals export did fetch
it. So every UUID osxphotos leaves incomplete gets one such export (into a
scratch dir, one UUID per call so files can be attributed) and, if that yields
a whole asset, its files replace osxphotos' partial ones in the batch with
report records of their own (`"exported_by": "photos-app"`), so n5's dedup
reads the UUID exactly as for an osxphotos file. osxphotos' JSON sidecar is
kept when the fallback media takes the name it was written for.

Not checked: RAW+JPEG completeness (Photos.sqlite doesn't say cheaply whether
an asset has a RAW resource). osxphotos exports both by default.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .dedup.photos import REPORT_NAME

MAX_ATTEMPTS = 3

PENDING, EXPORTED, DELIVERED, FAILED = "pending", "exported", "delivered", "failed"

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS uuid_state (
    uuid       TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    batch      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    files        INTEGER,
    bytes        INTEGER
);
"""

# Photos subtype for a Live Photo — same test osxphotos uses (photosdb.py).
_LIVE_SUBTYPE = 2

_VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
_SIDECAR_FLAGS = ("sidecar_xmp", "sidecar_json", "sidecar_exiftool", "sidecar_user")

OSXPHOTOS_EXPORT_ARGS = [
    "--download-missing", "--use-photokit",   # Photos.app fetches iCloud-only originals
    "--skip-edited",                          # decision #1: originals only
    # JSON only: dedup reads it for date/GPS the user corrected in Photos.
    # No XMP — on the 2026-10 backlog it carried no keywords or titles, only
    # Photos' rounded date/GPS (which would override exact camera EXIF in
    # Immich) and Meta-glasses serials as "captions".
    "--sidecar", "json",
    "--directory", "{created.year}/{created.mm}",
    "--retry", "2",
]


# Per-UUID AppleScript originals export. The timeout covers an iCloud
# download of a long video; `id` is Photos' scripting id, `<uuid>/L0/001`.
FALLBACK_TIMEOUT_S = 900
# Cap per batch, so a wholesale problem (Photos not running, permissions)
# can't turn one batch into hundreds of slow AppleScript calls. The rest
# stay `failed` and get another turn next run.
FALLBACK_MAX_PER_BATCH = 100

_APPLESCRIPT_EXPORT = f"""
on run argv
    set theDir to (item 2 of argv) as POSIX file
    with timeout of {FALLBACK_TIMEOUT_S} seconds
        tell application "Photos"
            export {{media item id (item 1 of argv)}} to theDir with using originals
        end tell
    end timeout
end run
"""

# Photos.app writes adjustment plists beside an edited original's export;
# they're not media and Immich ignores them.
_FALLBACK_SKIP_EXTS = {".aae", ".plist"}

# Core Data epoch (ZDATECREATED is seconds since 2001-01-01 UTC).
_APPLE_EPOCH = 978307200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- ledger ---------------------------------------------------------------


def open_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_LEDGER_SCHEMA)
    return conn


def enqueue(conn: sqlite3.Connection, uuids: list[str]) -> int:
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO uuid_state (uuid, status, updated_at) VALUES (?, ?, ?)",
        [(u, PENDING, _now()) for u in uuids],
    )
    conn.commit()
    return conn.total_changes - before


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute("SELECT status, count(*) FROM uuid_state GROUP BY status"))


def undelivered_batches(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM batch WHERE status = ? ORDER BY created_at", (EXPORTED,)
    )]


def next_uuids(conn: sqlite3.Connection, limit: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT uuid FROM uuid_state WHERE status IN (?, ?) AND attempts < ? "
        "ORDER BY status = ?, rowid LIMIT ?",   # pending first, then retries
        (PENDING, FAILED, MAX_ATTEMPTS, FAILED, limit),
    )]


# --- completeness ---------------------------------------------------------


@dataclass
class BatchCheck:
    complete: dict[str, list[Path]] = field(default_factory=dict)   # uuid → files
    incomplete: dict[str, str] = field(default_factory=dict)        # uuid → reason
    rescued: list[str] = field(default_factory=list)                # via Photos.app


def live_uuids(photos: sqlite3.Connection, uuids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(uuids), 500):
        chunk = uuids[i:i + 500]
        q = ",".join("?" * len(chunk))
        out.update(r[0] for r in photos.execute(
            f"SELECT ZUUID FROM ZASSET WHERE ZUUID IN ({q}) AND ZKIND = 0 "
            f"AND ZKINDSUBTYPE = ?", (*chunk, _LIVE_SUBTYPE),
        ))
    return out


def check_batch(records: list[dict], wanted: list[str], live: set[str]) -> BatchCheck:
    """Group the osxphotos report by UUID and decide which assets arrived whole.

    `records` is the parsed `osxphotos-report.json`. Files are taken from it
    (sidecars included) so an incomplete UUID's files can be removed exactly.
    """
    files: dict[str, list[Path]] = {u: [] for u in wanted}
    media: dict[str, list[Path]] = {u: [] for u in wanted}
    errors: dict[str, str] = {}
    for rec in records:
        uuid = rec.get("uuid")
        if uuid not in files:
            continue
        path = Path(rec.get("filename", ""))
        if rec.get("error") or rec.get("missing"):
            errors[uuid] = rec.get("error") or "missing in Photos library"
            continue
        if not (rec.get("exported") or rec.get("skipped")):
            continue
        files[uuid].append(path)
        if not any(rec.get(f) for f in _SIDECAR_FLAGS):
            media[uuid].append(path)

    check = BatchCheck()
    for uuid in wanted:
        present = [p for p in media[uuid] if p.is_file() and p.stat().st_size > 0]
        if uuid in errors:
            reason = errors[uuid]
        elif not present:
            reason = "nothing exported"
        elif uuid in live and not any(p.suffix.lower() in _VIDEO_EXTS for p in present):
            reason = "live photo without its video"
        else:
            check.complete[uuid] = files[uuid]
            continue
        check.incomplete[uuid] = reason
    return check


def prune_incomplete(batch_dir: Path, records: list[dict], check: BatchCheck) -> None:
    """Delete an incomplete UUID's files and drop its report records, so the
    batch holds only whole assets. These are export copies, never originals."""
    bad = set(check.incomplete)
    for rec in records:
        if rec.get("uuid") in bad:
            p = Path(rec.get("filename", ""))
            if p.is_file() and batch_dir in p.parents:
                p.unlink()
    kept = [r for r in records if r.get("uuid") not in bad]
    (batch_dir / REPORT_NAME).write_text(json.dumps(kept, indent=1))


# --- Photos.app fallback -------------------------------------------------


def photos_app_export(uuid: str, dest: Path, run: "Runner") -> tuple[list[Path], str | None]:
    """Export one asset's originals with Photos.app into an empty `dest`.
    Returns `(media files, error)`; error is None on a clean export."""
    dest.mkdir(parents=True, exist_ok=True)
    r = run(["osascript", "-e", _APPLESCRIPT_EXPORT, f"{uuid}/L0/001", str(dest)])
    files = sorted(
        p for p in dest.iterdir()
        if p.is_file() and p.stat().st_size > 0 and p.suffix.lower() not in _FALLBACK_SKIP_EXTS
    )
    if r.returncode != 0:
        return files, f"osascript exited {r.returncode}: {(r.stderr or '').strip()[-200:]}"
    return files, None


def _created_dir(photos: sqlite3.Connection, uuid: str) -> str:
    """`YYYY/MM` of the asset's capture date (UTC), as osxphotos' --directory
    template lays out a batch. Only cosmetic: promote files by EXIF date."""
    try:
        row = photos.execute("SELECT ZDATECREATED FROM ZASSET WHERE ZUUID = ?", (uuid,)).fetchone()
    except sqlite3.Error:
        row = None
    if not row or row[0] is None:
        return "photos-app"
    when = datetime.fromtimestamp(_APPLE_EPOCH + float(row[0]), timezone.utc)
    return f"{when.year:04d}/{when.month:02d}"


def _fallback_record(path: Path, uuid: str) -> dict:
    return {"filename": str(path), "exported": True, "skipped": False, "missing": False,
            "error": "", "uuid": uuid, "sidecar_json": False, "sidecar_xmp": False,
            "exported_by": "photos-app"}


def _whole(files: list[Path], is_live: bool) -> str | None:
    """Why a fallback export is not a whole asset, or None if it is."""
    if not files:
        return "nothing exported"
    if is_live and not any(p.suffix.lower() in _VIDEO_EXTS for p in files):
        return "live photo without its video"
    if is_live and all(p.suffix.lower() in _VIDEO_EXTS for p in files):
        return "live photo without its image"
    if len({p.suffix.lower() for p in files}) != len(files):
        return "unexpected export: " + ", ".join(p.name for p in files)
    return None


def photos_app_fallback(
    batch_dir: Path,
    records: list[dict],
    check: BatchCheck,
    photos: sqlite3.Connection,
    live: set[str],
    run: "Runner",
) -> tuple[list[dict], list[str]]:
    """Re-export each incomplete UUID through Photos.app and, where that gives
    a whole asset, swap its files into the batch. Returns the updated report
    records and the UUIDs rescued; `check.incomplete` reasons of the rest get
    the fallback's own reason appended. Re-run `check_batch` afterwards."""
    scratch = batch_dir.parent / f"{batch_dir.name}.photos-app"
    rescued: list[str] = []
    try:
        for uuid in list(check.incomplete)[:FALLBACK_MAX_PER_BATCH]:
            files, err = photos_app_export(uuid, scratch / uuid, run)
            why = err or _whole(files, uuid in live)
            if why:
                check.incomplete[uuid] += f"; Photos.app: {why}"
                continue
            records = _swap_in(batch_dir, records, uuid, files, photos)
            rescued.append(uuid)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if rescued:
        (batch_dir / REPORT_NAME).write_text(json.dumps(records, indent=1))
    return records, rescued


def _swap_in(
    batch_dir: Path, records: list[dict], uuid: str, files: list[Path],
    photos: sqlite3.Connection,
) -> list[dict]:
    """Replace osxphotos' partial files for `uuid` with the Photos.app export.

    The media take osxphotos' stem and directory when it exported an image
    (so its `<name>.json` sidecar still matches), else the original's own
    stem under `YYYY/MM`, de-duplicated like osxphotos does (`IMG_1 (1)`)."""
    mine = [r for r in records if r.get("uuid") == uuid]
    rest = [r for r in records if r.get("uuid") != uuid]
    inside = [Path(r.get("filename", "")) for r in mine]
    inside = [p for p in inside if batch_dir in p.parents]
    osx_media = [Path(r["filename"]) for r in mine
                 if Path(r.get("filename", "")) in inside
                 and not any(r.get(f) for f in _SIDECAR_FLAGS)
                 and Path(r["filename"]).suffix.lower() not in _VIDEO_EXTS]
    for r in mine:
        p = Path(r.get("filename", ""))
        if p in inside and p.is_file() and not any(r.get(f) for f in _SIDECAR_FLAGS):
            p.unlink()

    if osx_media:
        target, base = osx_media[0].parent, osx_media[0].stem
    else:
        target = batch_dir / _created_dir(photos, uuid)
        base = next((p for p in files if p.suffix.lower() not in _VIDEO_EXTS), files[0]).stem
    stem, n = base, 0
    while any((target / f"{stem}{p.suffix}").exists() for p in files):
        n += 1
        stem = f"{base} ({n})"
    target.mkdir(parents=True, exist_ok=True)

    placed = []
    for src in files:
        dst = target / f"{stem}{src.suffix}"
        shutil.move(str(src), dst)
        placed.append(dst)

    # Keep a sidecar only if it still sits beside a media file it was written for.
    names = {p.name for p in placed}
    kept = []
    for r in mine:
        p = Path(r.get("filename", ""))
        if not any(r.get(f) for f in _SIDECAR_FLAGS) or p not in inside:
            continue
        if p.parent == target and p.name.rsplit(".", 1)[0] in names and p.is_file():
            kept.append(r)
        elif p.is_file():
            p.unlink()
    return rest + kept + [_fallback_record(p, uuid) for p in placed]


def local_listing(batch_dir: Path) -> dict[str, int]:
    return {
        p.relative_to(batch_dir).as_posix(): p.stat().st_size
        for p in batch_dir.rglob("*") if p.is_file()
    }


# --- transport ------------------------------------------------------------


@dataclass(frozen=True)
class Remote:
    host: str
    root: str          # e.g. /mnt/tank/media/staging/photos

    @classmethod
    def parse(cls, dest: str) -> "Remote":
        host, sep, root = dest.partition(":")
        if not sep or not host or not root.startswith("/"):
            raise ValueError(f"--dest must look like host:/abs/path, got {dest!r}")
        return cls(host, root.rstrip("/"))

    def staging(self, batch: str) -> str:
        return f"{self.root}/.staging/{batch}"

    def ready(self, batch: str) -> str:
        return f"{self.root}/ready/{batch}"


Runner = Callable[[list[str]], subprocess.CompletedProcess]

# Unattended overnight runs: a dead tailnet path must fail fast instead of
# hanging an ssh/rsync forever, so the retry loop in the CLI gets a turn.
SSH_OPTS = ["-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=4", "-o", "BatchMode=yes"]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def remote_listing(remote: Remote, path: str, run: Runner = _run) -> dict[str, int] | None:
    """`{relpath: size}` of a remote dir, or None if it doesn't exist."""
    q = shlex.quote(path)
    r = run(["ssh", *SSH_OPTS, remote.host,
             f"test -d {q} && cd {q} && find . -type f -printf '%s %P\\n'"])
    if r.returncode != 0:
        return None
    out: dict[str, int] = {}
    for line in r.stdout.splitlines():
        size, _, rel = line.partition(" ")
        if rel:
            out[rel] = int(size)
    return out


class DeliveryError(RuntimeError):
    pass


def deliver(batch_dir: Path, remote: Remote, run: Runner = _run) -> None:
    """rsync a batch into `.staging/`, verify the listing, `mv` it to `ready/`.

    Idempotent: if `ready/<batch>` already matches (a previous run published
    but died before recording it), it's a success with nothing sent.
    """
    batch = batch_dir.name
    want = local_listing(batch_dir)
    if remote_listing(remote, remote.ready(batch), run) == want:
        return
    r = run(["ssh", *SSH_OPTS, remote.host, f"mkdir -p {shlex.quote(remote.root + '/.staging')} "
             f"{shlex.quote(remote.root + '/ready')}"])
    if r.returncode != 0:
        raise DeliveryError(f"ssh mkdir failed: {r.stderr.strip()}")
    r = run(["rsync", "-a", "--partial", "--delete", "--timeout=300",
             "-e", shlex.join(["ssh", *SSH_OPTS]),
             f"{batch_dir}/", f"{remote.host}:{remote.staging(batch)}/"])
    if r.returncode != 0:
        raise DeliveryError(f"rsync failed ({r.returncode}): {r.stderr.strip()[-500:]}")
    got = remote_listing(remote, remote.staging(batch), run)
    if got != want:
        raise DeliveryError("remote listing differs from local after rsync")
    staging, ready = shlex.quote(remote.staging(batch)), shlex.quote(remote.ready(batch))
    r = run(["ssh", *SSH_OPTS, remote.host, f"test ! -e {ready} && mv {staging} {ready}"])
    if r.returncode != 0:
        raise DeliveryError(f"publish (mv to ready/) failed: {r.stderr.strip()}")


# --- one batch ------------------------------------------------------------


def new_batch_id(conn: sqlite3.Connection) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = conn.execute("SELECT count(*) FROM batch").fetchone()[0] + 1
    return f"photos-{stamp}-{n:04d}"


def export_batch(
    conn: sqlite3.Connection,
    photos: sqlite3.Connection,
    uuids: list[str],
    export_root: Path,
    osxphotos: str = "osxphotos",
    run: Runner = _run,
    fallback_run: Runner | None = None,
) -> tuple[str | None, BatchCheck]:
    """Export `uuids` into a fresh batch dir. Returns `(batch_id, check)`;
    batch_id is None if nothing in it arrived whole. `fallback_run` runs the
    Photos.app export for UUIDs osxphotos left incomplete (the CLI passes the
    real runner on a Mac); None skips it."""
    batch = new_batch_id(conn)
    batch_dir = export_root / batch
    batch_dir.mkdir(parents=True)
    uuid_file = export_root / f"{batch}.uuids"
    uuid_file.write_text("".join(f"{u}\n" for u in uuids))
    report = batch_dir / REPORT_NAME
    r = run([osxphotos, "export", str(batch_dir), "--uuid-from-file", str(uuid_file),
             "--report", str(report), *OSXPHOTOS_EXPORT_ARGS])
    uuid_file.unlink(missing_ok=True)
    try:
        records = json.loads(report.read_text())
    except (OSError, json.JSONDecodeError):
        records = []
    live = live_uuids(photos, uuids)
    check = check_batch(records, uuids, live)
    if r.returncode != 0 and not check.complete:
        # osxphotos itself failed; Photos.app is unlikely to do better for a
        # whole batch, and the UUIDs get another turn next run.
        for u in uuids:
            check.incomplete.setdefault(u, f"osxphotos exited {r.returncode}")
    elif check.incomplete and fallback_run is not None:
        records, rescued = photos_app_fallback(batch_dir, records, check, photos, live, fallback_run)
        if rescued:
            reasons = check.incomplete
            check = check_batch(records, uuids, live)
            check.rescued = rescued
            for u in check.incomplete:
                check.incomplete[u] = reasons.get(u, check.incomplete[u])

    prune_incomplete(batch_dir, records, check)
    (batch_dir / ".osxphotos_export.db").unlink(missing_ok=True)

    now = _now()
    conn.executemany(
        "UPDATE uuid_state SET status=?, batch=NULL, attempts=attempts+1, "
        "last_error=?, updated_at=? WHERE uuid=?",
        [(FAILED, why, now, u) for u, why in check.incomplete.items()],
    )
    if not check.complete:
        shutil.rmtree(batch_dir)
        conn.commit()
        return None, check
    listing = local_listing(batch_dir)
    conn.execute(
        "INSERT INTO batch (id, status, created_at, files, bytes) VALUES (?, ?, ?, ?, ?)",
        (batch, EXPORTED, now, len(listing), sum(listing.values())),
    )
    conn.executemany(
        "UPDATE uuid_state SET status=?, batch=?, last_error=NULL, updated_at=? WHERE uuid=?",
        [(EXPORTED, batch, now, u) for u in check.complete],
    )
    conn.commit()
    return batch, check


def mark_delivered(conn: sqlite3.Connection, batch: str) -> None:
    now = _now()
    conn.execute("UPDATE batch SET status=?, delivered_at=? WHERE id=?", (DELIVERED, now, batch))
    conn.execute("UPDATE uuid_state SET status=?, updated_at=? WHERE batch=?",
                 (DELIVERED, now, batch))
    conn.commit()
