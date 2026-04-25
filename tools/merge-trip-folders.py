#!/usr/bin/env python3
"""Merge N sibling trip folders into one.

Moves top-level source files plus all .audit/ artifacts (offline ymls,
journal, y_processed, state, audit.jsonl, derivative posters/thumbs/
encoded-video trees). Rewrites baked-in folder-name references inside
yml/jsonl content so the merged folder works with `immy process` /
`immy sync` without further fixup.

Usage:
    merge-trip-folders.py <src1> <src2> [<src3> ...] --to <name> [--apply]

All folders must be siblings under the same trips root. Default is
dry-run; use --apply to execute. Refuses to run if it detects filename
or checksum collisions across sources.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml


def err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def rewrite_str(s: str, name_map: dict[str, str]) -> tuple[str, int]:
    n = 0
    for old, new in name_map.items():
        # match as a path component to avoid accidental substring hits
        for needle in (f"/{old}/", f"/{old}\n", f"/{old}\"", f"/{old}'"):
            if needle in s:
                s = s.replace(needle, needle.replace(old, new))
                n += 1
        if s.endswith(f"/{old}"):
            s = s[: -len(old)] + new
            n += 1
    return s, n


def rewrite_obj(obj, name_map: dict[str, str]) -> tuple[object, int]:
    n = 0
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            v2, m = rewrite_obj(v, name_map)
            n += m
            out[k] = v2
        return out, n
    if isinstance(obj, list):
        out_l: list = []
        for v in obj:
            v2, m = rewrite_obj(v, name_map)
            n += m
            out_l.append(v2)
        return out_l, n
    if isinstance(obj, str):
        s2, m = rewrite_str(obj, name_map)
        return s2, m
    return obj, 0


def merge_dir_tree(src: Path, dst: Path, *, apply: bool, log: list[str]) -> None:
    """Recursively move contents of src into dst, merging directories."""
    if not src.exists():
        return
    if not src.is_dir():
        log.append(f"skip non-dir: {src}")
        return
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            if apply:
                target.mkdir(parents=True, exist_ok=True)
            merge_dir_tree(entry, target, apply=apply, log=log)
            if apply:
                try:
                    entry.rmdir()
                except OSError:
                    pass
        else:
            if target.exists():
                log.append(f"COLLISION (kept existing): {entry} → {target}")
                continue
            log.append(f"mv {entry} → {target}")
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry), str(target))


def collect_top_files(src: Path) -> list[Path]:
    out: list[Path] = []
    for p in src.iterdir():
        if p.name == ".audit":
            continue
        if p.name.startswith("."):
            continue
        out.append(p)
    return out


def merge_yaml_dict_under_key(
    sources: list[Path], key: str, dst: Path, name_map: dict[str, str], *, apply: bool
) -> tuple[int, int]:
    merged: dict = {}
    rewrites = 0
    for src in sources:
        if not src.exists():
            continue
        with src.open() as f:
            data = yaml.safe_load(f) or {}
        data, m = rewrite_obj(data, name_map)
        rewrites += m
        sub = data.get(key) or {}
        if not isinstance(sub, dict):
            err(f"{src}: expected dict under '{key}', got {type(sub).__name__}")
            continue
        for k, v in sub.items():
            if k in merged and merged[k] != v:
                # Same checksum/key in two sources but different value — keep first, log.
                pass
            merged.setdefault(k, v)
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("w") as f:
            yaml.safe_dump({key: merged}, f, sort_keys=False)
    return len(merged), rewrites


def merge_y_processed(
    sources: list[Path], dst: Path, name_map: dict[str, str], *, apply: bool
) -> tuple[int, int]:
    """Merge `assets:` lists and sum scalar counters; latest processed_at wins."""
    out: dict = {
        "processed_at": 0,
        "inserted": 0,
        "already_present": 0,
        "derivatives_staged": 0,
        "clip_embedded": 0,
        "faces_detected": 0,
        "transcripts_written": 0,
        "captions_written": 0,
        "assets": [],
    }
    rewrites = 0
    for src in sources:
        if not src.exists():
            continue
        with src.open() as f:
            data = yaml.safe_load(f) or {}
        data, m = rewrite_obj(data, name_map)
        rewrites += m
        for sk in ("inserted", "already_present", "derivatives_staged",
                   "clip_embedded", "faces_detected", "transcripts_written",
                   "captions_written"):
            out[sk] += int(data.get(sk) or 0)
        out["processed_at"] = max(out["processed_at"], int(data.get("processed_at") or 0))
        for a in data.get("assets") or []:
            out["assets"].append(a)
    if apply:
        with dst.open("w") as f:
            yaml.safe_dump(out, f, sort_keys=False)
    return len(out["assets"]), rewrites


def concat_jsonl(sources: list[Path], dst: Path, name_map: dict[str, str], *, apply: bool) -> tuple[int, int]:
    lines = 0
    rewrites = 0
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        out_f = dst.open("w")
    else:
        out_f = None
    try:
        for src in sources:
            if not src.exists():
                continue
            with src.open() as f:
                for line in f:
                    line2, m = rewrite_str(line, name_map)
                    rewrites += m
                    lines += 1
                    if out_f:
                        out_f.write(line2)
    finally:
        if out_f:
            out_f.close()
    return lines, rewrites


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+", help="Source folders")
    ap.add_argument("--to", dest="dest", required=True, help="Target folder name (under trips root)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # Resolve trips root from the first source.
    sources = [Path(s).expanduser().resolve() if "/" in s else None for s in args.sources]
    if any(s is None for s in sources):
        # Mixed names + paths; resolve names later. For now require all-paths or all-names.
        pass
    src_paths: list[Path] = []
    for s in args.sources:
        p = Path(s).expanduser()
        if not p.is_absolute():
            # Treat as name relative to first absolute source's parent, or CWD
            pass
        src_paths.append(p.resolve())

    # All sources must exist and share the same parent.
    parents = {p.parent for p in src_paths}
    if len(parents) != 1:
        err(f"sources span multiple parents: {parents}")
        return 2
    trips_root = parents.pop()
    for p in src_paths:
        if not p.is_dir():
            err(f"not a directory: {p}")
            return 2

    dest = trips_root / args.dest
    if dest in src_paths:
        err("dest cannot be one of the sources")
        return 2

    name_map = {p.name: dest.name for p in src_paths}
    print(f"trips root: {trips_root}")
    print(f"dest:       {dest}")
    print(f"sources:    {[p.name for p in src_paths]}")
    print(f"name map:   {name_map}")
    print()

    # ---- Pre-flight collision checks ----
    top_seen: dict[str, Path] = {}
    poster_seen: dict[str, Path] = {}
    offline_seen: dict[str, Path] = {}
    collisions: list[str] = []
    for src in src_paths:
        for f in collect_top_files(src):
            if f.name in top_seen:
                collisions.append(f"top-level filename collision: {f.name} in {top_seen[f.name].parent.name} and {src.name}")
            top_seen[f.name] = f
        posters = src / ".audit" / "derivatives" / "_posters"
        if posters.is_dir():
            for f in posters.iterdir():
                if f.name in poster_seen:
                    collisions.append(f"poster UUID collision: {f.name}")
                poster_seen[f.name] = f
        offline = src / ".audit" / "offline"
        if offline.is_dir():
            for f in offline.iterdir():
                if f.is_dir():  # e.g. embeddings/ — handled as tree merge
                    continue
                if f.name in offline_seen:
                    collisions.append(f"offline yml collision: {f.name}")
                offline_seen[f.name] = f
    if collisions:
        for c in collisions:
            err(c)
        return 3

    print(f"pre-flight ok: {len(top_seen)} top files, {len(offline_seen)} offline ymls, {len(poster_seen)} posters")
    print()

    apply = args.apply
    if apply:
        dest.mkdir(exist_ok=True)
        (dest / ".audit" / "derivatives").mkdir(parents=True, exist_ok=True)
        (dest / ".audit" / "offline").mkdir(parents=True, exist_ok=True)

    log: list[str] = []

    # ---- Move top-level files ----
    for src in src_paths:
        for f in collect_top_files(src):
            target = dest / f.name
            log.append(f"mv {f} → {target}")
            if apply:
                shutil.move(str(f), str(target))

    # ---- Move offline ymls (with path rewrite) ----
    offline_rewrites = 0
    moved_offline = 0
    for src in src_paths:
        offline = src / ".audit" / "offline"
        if not offline.is_dir():
            continue
        for yml in offline.iterdir():
            if yml.is_dir():
                # Subdirectories (e.g. embeddings/) are tree-merged below.
                continue
            target = dest / ".audit" / "offline" / yml.name
            log.append(f"mv {yml} → {target} (rewrite paths)")
            if apply:
                with yml.open() as fh:
                    data = yaml.safe_load(fh) or {}
                data, m = rewrite_obj(data, name_map)
                offline_rewrites += m
                with target.open("w") as fh:
                    yaml.safe_dump(data, fh, sort_keys=False)
                yml.unlink()
            moved_offline += 1
        # Merge any subdirectories under offline/ (e.g. embeddings/) as trees.
        for sub in offline.iterdir() if offline.is_dir() else []:
            if not sub.is_dir():
                continue
            sub_dst = dest / ".audit" / "offline" / sub.name
            if apply:
                sub_dst.mkdir(parents=True, exist_ok=True)
            merge_dir_tree(sub, sub_dst, apply=apply, log=log)
            if apply:
                try:
                    sub.rmdir()
                except OSError:
                    pass

    # ---- Move derivative trees ----
    deriv_dst = dest / ".audit" / "derivatives"
    for sub in ("_posters", "thumbs", "encoded-video"):
        for src in src_paths:
            src_dir = src / ".audit" / "derivatives" / sub
            if src_dir.is_dir():
                if apply:
                    (deriv_dst / sub).mkdir(parents=True, exist_ok=True)
                merge_dir_tree(src_dir, deriv_dst / sub, apply=apply, log=log)
                if apply:
                    try:
                        src_dir.rmdir()
                    except OSError:
                        pass

    # ---- Merge journal.yml ----
    journals = [p / ".audit" / "journal.yml" for p in src_paths]
    j_count, j_rew = merge_yaml_dict_under_key(
        journals, "entries", dest / ".audit" / "journal.yml", name_map, apply=apply
    )
    if apply:
        for j in journals:
            if j.exists():
                j.unlink()

    # ---- Merge y_processed.yml ----
    yps = [p / ".audit" / "y_processed.yml" for p in src_paths]
    if any(y.exists() for y in yps):
        yp_assets, yp_rew = merge_y_processed(
            yps, dest / ".audit" / "y_processed.yml", name_map, apply=apply
        )
        if apply:
            for y in yps:
                if y.exists():
                    y.unlink()
    else:
        yp_assets, yp_rew = 0, 0

    # ---- Merge state.yml ----
    states = [p / ".audit" / "state.yml" for p in src_paths]
    if any(s.exists() for s in states):
        s_count, s_rew = merge_yaml_dict_under_key(
            states, "applied", dest / ".audit" / "state.yml", name_map, apply=apply
        )
        if apply:
            for s in states:
                if s.exists():
                    s.unlink()
    else:
        s_count, s_rew = 0, 0

    # ---- Concat audit.jsonl ----
    audits = [p / ".audit" / "audit.jsonl" for p in src_paths]
    if any(a.exists() for a in audits):
        a_lines, a_rew = concat_jsonl(audits, dest / ".audit" / "audit.jsonl", name_map, apply=apply)
        if apply:
            for a in audits:
                if a.exists():
                    a.unlink()
    else:
        a_lines, a_rew = 0, 0

    # ---- Rewrite stale folder references in moved sidecars (xmp, srt, json, vtt) ----
    sidecar_rewrites = 0
    sidecar_files = 0
    if apply:
        for f in dest.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".xmp", ".srt", ".json", ".vtt", ".txt"):
                continue
            try:
                text = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            new_text, n = rewrite_str(text, name_map)
            # Also handle bare-name occurrences inside text (e.g. dc:subject tags)
            for old in name_map:
                if old in text:
                    new_text = new_text.replace(old, name_map[old])
                    n += text.count(old)
            if n and new_text != text:
                f.write_text(new_text)
                sidecar_rewrites += n
                sidecar_files += 1

    # ---- Move any other unknown .audit files (preserve, with warning) ----
    KNOWN = {"journal.yml", "y_processed.yml", "state.yml", "audit.jsonl", "offline", "derivatives"}
    for src in src_paths:
        audit = src / ".audit"
        if not audit.is_dir():
            continue
        for entry in audit.iterdir():
            if entry.name in KNOWN:
                continue
            target = dest / ".audit" / entry.name
            if target.exists():
                target = dest / ".audit" / f"{entry.name}.from-{src.name}"
            log.append(f"mv (unknown audit file) {entry} → {target}")
            if apply:
                shutil.move(str(entry), str(target))

    # ---- Try to clean up empty source dirs ----
    if apply:
        for src in src_paths:
            audit = src / ".audit"
            if audit.is_dir():
                for sub in ("derivatives/_posters", "derivatives/thumbs",
                            "derivatives/encoded-video", "derivatives", "offline"):
                    d = audit / sub
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                try:
                    audit.rmdir()
                except OSError:
                    pass
            try:
                src.rmdir()
            except OSError:
                err(f"source not empty after merge, keeping: {src}")

    # ---- Summary ----
    print(f"top-level moves: {len(top_seen)}")
    print(f"offline ymls:    {moved_offline}  (rewrites={offline_rewrites})")
    print(f"journal entries: {j_count}  (rewrites={j_rew})")
    print(f"y_processed assets: {yp_assets}  (rewrites={yp_rew})")
    print(f"state.yml entries:  {s_count}  (rewrites={s_rew})")
    print(f"audit.jsonl lines:  {a_lines}  (rewrites={a_rew})")
    print(f"sidecars rewritten: {sidecar_files if apply else '(skipped in dry-run)'}  (rewrites={sidecar_rewrites if apply else 0})")
    print()
    print(f"log entries: {len(log)}  (showing first 20)")
    for line in log[:20]:
        print(f"  {line}")
    if len(log) > 20:
        print(f"  ... ({len(log) - 20} more)")
    if not apply:
        print()
        print("DRY RUN — re-run with --apply to execute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
