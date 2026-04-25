#!/usr/bin/env python3
"""Split a trip folder by per-day date into named sibling buckets.

Like split-by-month.py but groups by full YYYYMMDD and lets the caller
map each date to a target bucket name (so several dates can land in the
same sibling folder). Moves source files plus their .audit/offline yml
and derivative files keyed on asset_id; rewrites baked-in paths inside
the moved ymls.

Buckets are written as sibling folders under <src>'s parent. Use
merge-trip-folders.py afterwards to fold them into existing trip folders.

Usage:
    split-by-date.py <src> --map YYYYMMDD=bucket-name [--map ...] [--apply]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml


DATE_RE = re.compile(r"_(\d{8})(?:_|\d{6})")


def file_yyyymmdd(path: Path) -> str | None:
    m = DATE_RE.search(path.name)
    return m.group(1) if m else None


def find_derivative_files(audit_root: Path, asset_id: str) -> list[Path]:
    out: list[Path] = []
    deriv = audit_root / "derivatives"
    if not deriv.is_dir():
        return out
    for p in deriv.rglob(f"*{asset_id}*"):
        if p.is_file():
            out.append(p)
    return out


def rewrite_yaml_paths(data, old_root: str, new_root: str):
    if isinstance(data, dict):
        return {k: rewrite_yaml_paths(v, old_root, new_root) for k, v in data.items()}
    if isinstance(data, list):
        return [rewrite_yaml_paths(v, old_root, new_root) for v in data]
    if isinstance(data, str) and old_root in data:
        return data.replace(old_root, new_root)
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--map", action="append", default=[],
                    help="YYYYMMDD=bucket-name (repeatable)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    src: Path = args.src.resolve()
    if not src.is_dir():
        sys.exit(f"not a directory: {src}")
    parent = src.parent

    date_map: dict[str, str] = {}
    for m in args.map:
        if "=" not in m:
            sys.exit(f"bad --map: {m}")
        d, name = m.split("=", 1)
        date_map[d.strip()] = name.strip()

    if not date_map:
        sys.exit("no --map entries")

    # Collect file groups by date
    moves: dict[str, list[Path]] = {}
    unmapped: dict[str, int] = {}
    for p in src.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        ymd = file_yyyymmdd(p)
        if ymd is None:
            continue
        if ymd not in date_map:
            unmapped[ymd] = unmapped.get(ymd, 0) + 1
            continue
        moves.setdefault(ymd, []).append(p)

    # Index offline ymls by original_file_name + asset_id.
    # Same filename may appear in multiple ymls (re-processed assets with
    # different checksums) — track all of them.
    offline_dir = src / ".audit" / "offline"
    name_to_ymls: dict[str, list[Path]] = {}
    yml_to_asset: dict[Path, str] = {}
    if offline_dir.is_dir():
        for yml in offline_dir.glob("*.yml"):
            try:
                with yml.open() as f:
                    data = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"WARN: cannot parse {yml}: {e}", file=sys.stderr)
                continue
            asset = data.get("asset") or {}
            fn = asset.get("original_file_name")
            if fn:
                name_to_ymls.setdefault(fn, []).append(yml)
                if asset.get("id"):
                    yml_to_asset[yml] = asset["id"]

    # Plan
    print(f"src: {src}")
    print(f"parent: {parent}")
    print()
    grand = 0
    for ymd in sorted(moves):
        bucket = date_map[ymd]
        target = parent / bucket
        files = moves[ymd]
        grand += len(files)
        print(f"[{ymd}] {len(files):4d} files → {target}")
    if unmapped:
        print()
        print("UNMAPPED dates (left in src):")
        for d, n in sorted(unmapped.items()):
            print(f"  {d}: {n} files")
    print(f"\nTOTAL files to move: {grand}")

    if not args.apply:
        print("\n(dry-run — pass --apply to execute)")
        return 0

    # Execute
    moved = 0
    yml_moved = 0
    deriv_moved = 0
    for ymd, files in moves.items():
        bucket = date_map[ymd]
        target = parent / bucket
        target_audit = target / ".audit"
        target_offline = target_audit / "offline"
        target_offline.mkdir(parents=True, exist_ok=True)

        for f in files:
            dst = target / f.name
            if dst.exists():
                print(f"SKIP (exists): {dst}")
                continue
            shutil.move(str(f), str(dst))
            moved += 1

            for yml in name_to_ymls.get(f.name, []):
                if not yml.exists():
                    continue
                with yml.open() as fh:
                    data = yaml.safe_load(fh)
                data = rewrite_yaml_paths(data, str(src), str(target))
                new_yml = target_offline / yml.name
                with new_yml.open("w") as fh:
                    yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
                yml.unlink()
                yml_moved += 1

                asset_id = yml_to_asset.get(yml)
                if asset_id:
                    for d in find_derivative_files(src / ".audit", asset_id):
                        rel = d.relative_to(src / ".audit" / "derivatives")
                        new_d = target_audit / "derivatives" / rel
                        new_d.parent.mkdir(parents=True, exist_ok=True)
                        if new_d.exists():
                            continue
                        shutil.move(str(d), str(new_d))
                        deriv_moved += 1

    print(f"\nmoved {moved} src files, {yml_moved} ymls, {deriv_moved} derivatives")
    print("NOTE: src/.audit/journal.yml + y_processed.yml NOT split — re-run `immy process` per target.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
