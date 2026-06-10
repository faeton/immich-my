#!/usr/bin/env python3
"""Re-push descriptions Immich's metadata refresh clobbered.

Immich (v2.x) rebuilds `asset_exif` from file tags on metadata
extraction and overwrites every field not in `lockedProperties`.
Descriptions written by immy via direct SQL carry no lock and no XMP
sidecar entry, so a library rescan replaces them with whatever the
camera embedded: '' (most videos), 'default' (DJI), a 'DCIM\\...' path
or the file's own name (Insta360). 2026-06: one scan wiped 338 synced
descriptions this way.

This tool compares the server's descriptions against the local offline
sink (source of truth) and re-pushes diverged ones. The durable path
differs by asset type (verified against immich v2.7.5 source):

- In theory `PUT /api/assets/{id}` is the blessed path: Immich writes
  the description, locks the field, and bakes it into an .xmp sidecar
  that the photo extraction path prefers. In practice the API route is
  SELF-DEFEATING on this deployment for BOTH types: SidecarWrite
  unconditionally unlocks the field and queues re-extraction; the video
  path reads ONLY container tags (sidecars are images-only), and the
  image sidecar write itself fails against the NAS, so re-extraction
  re-imports embedded camera junk. (2026-06: 197 video then 124 image
  descriptions pushed via PUT were wiped again within minutes.)
- The durable, non-file-mutating mechanism is direct SQL: set
  `description` and append 'description' to
  `asset_exif."lockedProperties"` in one statement — no job cycle runs,
  and any future metadata refresh skips locked fields.

Safety rule: a server description is only overwritten when it is
empty, 'AI: '-prefixed, camera boilerplate ('default', 'DCIM\\...',
equals its own filename/stem), or an old transcript on a video whose
transcript record the journal owns. Anything else is listed for manual
review and left untouched.

Dry-run by default — pass `--apply` to push. Plan/applied logs land in
the --work dir.

Usage:
  tools/reconcile-descriptions.py            # dry run, all trips
  tools/reconcile-descriptions.py --apply
  tools/reconcile-descriptions.py --trip 2025-11-pacific-vanuatu
  TRIPS_ROOT=/other/path tools/reconcile-descriptions.py
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IMMY_SRC = SCRIPT_DIR.parent / "immy" / "src"
sys.path.insert(0, str(IMMY_SRC))

# Needs immy's deps (yaml) — re-exec under the immy venv when launched
# with a bare system python.
_VENV_PY = SCRIPT_DIR.parent / "immy" / ".venv" / "bin" / "python"
if _VENV_PY.is_file() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(Path(__file__).resolve()),
                             *sys.argv[1:]])

import yaml  # noqa: E402

from immy.captions import is_ai_description, is_camera_boilerplate  # noqa: E402
from immy.config import load as load_immy_config  # noqa: E402
from immy.journal import Journal, journal_path  # noqa: E402
from immy import pg as pg_mod  # noqa: E402

# Set description + lock it in one statement. The lock is what makes a
# VIDEO description survive metadata re-extraction (v2.7.5 honours
# lockedProperties with behaviour 'skip'); nothing in this path queues
# a job that would unlock it again.
_SQL_SET_AND_LOCK = """
UPDATE asset_exif
SET description = %(description)s,
    "lockedProperties" = CASE
      WHEN 'description' = ANY(coalesce("lockedProperties", '{}'))
        THEN "lockedProperties"
      ELSE array_append(coalesce("lockedProperties", '{}'), 'description')
    END
WHERE "assetId" = %(asset_id)s
"""

_SQL_HAS_LOCK_COLUMN = """
SELECT 1 FROM information_schema.columns
WHERE table_name = 'asset_exif' AND column_name = 'lockedProperties'
"""

TRIPS_ROOT = Path(os.environ.get("TRIPS_ROOT", str(Path.home() / "Media" / "Trips")))


def load_config(path: Path) -> tuple[str, str]:
    cfg = yaml.safe_load(path.read_text())
    im = cfg["immich"]
    return im["url"].rstrip("/"), im["api_key"]


def api(url: str, key: str, method: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(), method=method,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    return json.load(urllib.request.urlopen(req, context=ctx, timeout=60))


def collect_local_truth(trips: list[Path]) -> tuple[dict[str, str], set[str]]:
    """Returns ({asset_id: sink description}, {video ids whose transcript
    the journal owns — their old machine excerpts are overwritable})."""
    sink: dict[str, str] = {}
    vid_owned: set[str] = set()
    for trip in trips:
        d = trip / ".audit" / "offline"
        if not d.is_dir():
            continue
        jr = Journal.load(trip) if journal_path(trip).is_file() else None
        for p in d.glob("*.yml"):
            e = yaml.safe_load(p.read_text()) or {}
            a = e.get("asset") or {}
            if "id" not in a:
                continue
            sink[a["id"]] = ((e.get("exif") or {}).get("description") or "").strip()
            if (a.get("asset_type") == "VIDEO" and jr is not None
                    and "transcript" in (jr.entries.get(p.stem) or {})):
                vid_owned.add(a["id"])
    return sink, vid_owned


def overwritable(db: str, file_name: str, asset_id: str, vid_owned: set[str]) -> bool:
    return (not db or is_ai_description(db)
            or is_camera_boilerplate(db, file_name)
            or asset_id in vid_owned)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trip", help="reconcile a single trip folder name")
    ap.add_argument("--apply", action="store_true", help="push updates (default: dry run)")
    ap.add_argument("--work", default=str(Path.home() / ".immy" / "reconcile-descriptions"))
    ap.add_argument("--config", default=str(Path.home() / ".immy" / "config.yml"))
    args = ap.parse_args()

    url, key = load_config(Path(args.config))
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    if args.trip:
        trips = [TRIPS_ROOT / args.trip]
        if not trips[0].is_dir():
            sys.exit(f"no such trip: {trips[0]}")
    else:
        trips = sorted(d for d in TRIPS_ROOT.iterdir()
                       if d.is_dir() and not d.name.startswith("."))

    sink, vid_owned = collect_local_truth(trips)
    print(f"local truth: {len(sink)} assets across {len(trips)} trip(s)")

    plan, review = [], []
    for typ in ("VIDEO", "IMAGE"):
        page = 1
        while True:
            r = api(url, key, "POST", "/api/search/metadata",
                    {"type": typ, "size": 1000, "page": page, "withExif": True})
            for a in r["assets"]["items"]:
                sd = sink.get(a["id"])
                if sd is None:
                    continue
                db = ((a.get("exifInfo") or {}).get("description") or "").strip()
                if db == sd:
                    continue
                row = {"id": a["id"], "name": a["originalFileName"],
                       "type": typ, "db": db, "sink": sd}
                if overwritable(db, a["originalFileName"], a["id"], vid_owned):
                    plan.append(row)
                else:
                    review.append(row)
            nxt = r["assets"].get("nextPage")
            if not nxt:
                break
            page = int(nxt)

    (work / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    (work / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=1))
    print(f"would update: {len(plan)} via SQL+lock  ·  review (untouched): {len(review)}")
    for row in review[:10]:
        print(f"  REVIEW {row['name']}\n    DB:   {row['db'][:80]!r}\n    sink: {row['sink'][:80]!r}")

    if not args.apply:
        if plan:
            print(f"\ndry run — plan at {work / 'plan.json'}; re-run with --apply to push.")
        return

    cfg = load_immy_config(Path(args.config))
    if cfg.pg is None:
        sys.exit("`pg:` missing from the immy config (description lock is SQL-only)")
    conn = pg_mod.connect(cfg.pg)
    with conn.cursor() as cur:
        cur.execute(_SQL_HAS_LOCK_COLUMN)
        if cur.fetchone() is None:
            sys.exit('asset_exif."lockedProperties" not found — Immich too old/new '
                     "for the lock-based path; aborting before any write.")

    pushed = 0
    try:
        for row in plan:
            with conn.cursor() as cur:
                cur.execute(_SQL_SET_AND_LOCK, {
                    "asset_id": row["id"], "description": row["sink"],
                })
            conn.commit()
            pushed += 1
    finally:
        conn.close()
    (work / "applied.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    print(f"pushed {pushed} description(s) via SQL+lock.")


if __name__ == "__main__":
    main()
