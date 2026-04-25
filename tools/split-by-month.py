#!/usr/bin/env python3
"""Split a misdated trip folder into per-month sibling folders.

Moves source files (insv/insp/dng/mp4/srt) plus their .audit/offline/<sha>.yml
and all derivative files keyed on asset_id. Rewrites absolute paths inside
each yml to point at the new location. Files already inside subdirs of
.audit/ are left alone — only entries in <root>/.audit/offline + the derivative
files referenced by moved ymls are touched.

Usage:
    split-by-month.py <source-folder> <dest-parent> [--apply]
        [--keep-months 202311,202312] [--prefix 360-]

Default is dry-run. Without --apply, prints planned moves only.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

import yaml


DATE_RE = re.compile(r"_(\d{8})_")
SOURCE_EXTS = {".insv", ".insp", ".dng", ".mp4", ".jpg", ".jpeg", ".heic"}
SRT_RE = re.compile(r"\.[a-z]{2,3}\.srt$", re.IGNORECASE)


def file_yyyymm(path: Path) -> str | None:
    m = DATE_RE.search(path.name)
    if not m:
        return None
    return m.group(1)[:6]


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_derivative_files(audit_root: Path, asset_id: str) -> list[Path]:
    """All files inside .audit/derivatives that contain the asset_id."""
    out: list[Path] = []
    deriv = audit_root / "derivatives"
    if not deriv.is_dir():
        return out
    for p in deriv.rglob(f"*{asset_id}*"):
        if p.is_file():
            out.append(p)
    return out


def rewrite_yaml_paths(data: dict, old_root: str, new_root: str) -> dict:
    """Recursively replace old_root prefix with new_root in any string values."""
    def _walk(obj):
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(v) for v in obj]
        if isinstance(obj, str) and old_root in obj:
            return obj.replace(old_root, new_root)
        return obj
    return _walk(data)


def plan(src: Path, dest_parent: Path, keep_months: set[str], prefix: str):
    audit = src / ".audit"
    offline_dir = audit / "offline"

    # Index source files by (basename-without-extension, sidecar?) — but
    # simplest: every regular file in src that has a recognizable date.
    moves: dict[str, list[Path]] = {}  # yyyymm → [paths to move]
    for p in src.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith("."):  # .DS_Store etc — ignore
            continue
        ym = file_yyyymm(p)
        if ym is None:
            continue
        if ym in keep_months:
            continue
        moves.setdefault(ym, []).append(p)

    # Build sha → asset_id map by reading every offline yml once. We only
    # need the entries whose original_file_name corresponds to a file we
    # plan to move.
    sha_to_yml: dict[str, Path] = {}
    name_to_sha: dict[str, str] = {}
    sha_to_asset: dict[str, str] = {}
    for yml in offline_dir.glob("*.yml") if offline_dir.is_dir() else []:
        try:
            with yml.open() as f:
                data = yaml.safe_load(f)
        except Exception as e:
            print(f"WARN: cannot parse {yml}: {e}", file=sys.stderr)
            continue
        sha = (data.get("asset") or {}).get("checksum") or yml.stem
        fn = (data.get("asset") or {}).get("original_file_name")
        aid = (data.get("asset") or {}).get("id")
        sha_to_yml[sha] = yml
        if fn:
            name_to_sha[fn] = sha
        if aid:
            sha_to_asset[sha] = aid

    return moves, sha_to_yml, name_to_sha, sha_to_asset


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("dest_parent", type=Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--keep-months",
        default="",
        help="comma-separated YYYYMM list to keep in src (e.g. 202311,202312)",
    )
    ap.add_argument(
        "--prefix",
        default="",
        help="prefix for new folder names; default '<src.basename>-bucket-'",
    )
    ap.add_argument(
        "--suffix",
        default="-360",
        help="suffix for new month folder names (default -360)",
    )
    args = ap.parse_args()

    src: Path = args.src.resolve()
    dest_parent: Path = args.dest_parent.resolve()
    keep_months = {m.strip() for m in args.keep_months.split(",") if m.strip()}

    if not src.is_dir():
        sys.exit(f"not a directory: {src}")

    moves, sha_to_yml, name_to_sha, sha_to_asset = plan(
        src, dest_parent, keep_months, args.prefix
    )

    if not moves:
        print("nothing to move")
        return

    # Print plan
    total = 0
    for ym in sorted(moves):
        files = moves[ym]
        total += len(files)
        bucket = f"{ym[:4]}-{ym[4:6]}{args.suffix}"
        target = dest_parent / bucket
        print(f"\n[{ym}] {len(files):4d} files → {target}")
        for f in sorted(files)[:5]:
            print(f"   {f.name}")
        if len(files) > 5:
            print(f"   …(+{len(files) - 5} more)")
    print(f"\nTOTAL src files: {total}")
    print(f"keep months: {sorted(keep_months) or '(none)'}")

    if not args.apply:
        print("\n(dry-run — pass --apply to execute)")
        return

    # Execute
    moved_count = 0
    yml_moved = 0
    deriv_moved = 0
    for ym in sorted(moves):
        bucket = f"{ym[:4]}-{ym[4:6]}{args.suffix}"
        target = dest_parent / bucket
        target.mkdir(parents=True, exist_ok=True)
        target_audit = target / ".audit"
        target_offline = target_audit / "offline"
        target_offline.mkdir(parents=True, exist_ok=True)

        for f in moves[ym]:
            dst = target / f.name
            if dst.exists():
                print(f"SKIP (exists): {dst}")
                continue
            shutil.move(str(f), str(dst))
            moved_count += 1

            # Move offline yml + derivatives if this is a primary asset.
            sha = name_to_sha.get(f.name)
            if not sha:
                # Try by computing checksum (if file lives in offline by sha)
                # The Immich import uses sha1; we matched by filename above
                # already, so missing here means yml wasn't generated for
                # this file (e.g. .srt sidecars, .dng pairs).
                continue
            yml = sha_to_yml.get(sha)
            asset_id = sha_to_asset.get(sha)
            if not yml or not yml.exists():
                continue
            # Read + rewrite paths
            with yml.open() as fh:
                data = yaml.safe_load(fh)
            data = rewrite_yaml_paths(data, str(src), str(target))
            new_yml_path = target_offline / yml.name
            with new_yml_path.open("w") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
            yml.unlink()
            yml_moved += 1

            # Move derivative files
            if asset_id:
                deriv_files = find_derivative_files(src / ".audit", asset_id)
                for d in deriv_files:
                    rel = d.relative_to(src / ".audit" / "derivatives")
                    new_d = target_audit / "derivatives" / rel
                    new_d.parent.mkdir(parents=True, exist_ok=True)
                    if new_d.exists():
                        print(f"SKIP deriv (exists): {new_d}")
                        continue
                    shutil.move(str(d), str(new_d))
                    deriv_moved += 1

    print(
        f"\nmoved {moved_count} source files, {yml_moved} ymls, {deriv_moved} derivative files"
    )
    print("NOTE: src/.audit/journal.yml + y_processed.yml were NOT split.")
    print("      Consider re-running `immy process` on each new folder to refresh.")


if __name__ == "__main__":
    main()
