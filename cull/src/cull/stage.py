"""`cull stage`: hardlink kept items into trips/<trip>/<src>/<rel>, per trip,
every check before any link. Never copies, never deletes. A receipt per trip
(`.audit/cull-staged.json`) records what was linked from where, so a re-run
is a no-op and an item that was dropped or moved after staging is reported
as stale until its links are removed by hand."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import decisions as dec_mod
from .core.jsonio import now_iso, read_json, write_json_atomic
from .scan import load_items

RECEIPT = Path(".audit") / "cull-staged.json"


@dataclass
class Link:
    src: Path
    dst: Path


@dataclass
class ItemPlan:
    id: str
    links: list[Link]
    skips: list[Link]
    record: dict


@dataclass
class TripPlan:
    trip: str
    items: list[ItemPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.stale

    @property
    def nbytes(self) -> int:
        return sum(lk.src.stat().st_size for ip in self.items for lk in ip.links)


@dataclass
class Plan:
    trips: list[TripPlan]
    no_trip: list[str]


def _live(entry: dict) -> bool:
    return any(Path(p).exists() for p in entry.get("dst", []))


def _receipts(trips_root: Path) -> dict[str, dict]:
    out = {}
    for d in sorted(p for p in trips_root.iterdir() if p.is_dir()):
        r = read_json(d / RECEIPT, None)
        if r:
            out[d.name] = r
    return out


def plan(work: Path, trips_root: Path, *, existing_paths=None,
         immich_prefix: str = "/mnt/external/originals") -> Plan:
    doc = load_items(work)
    sources = {k: Path(v) for k, v in doc["sources"].items()}
    items = {i["id"]: i for i in doc["items"]}
    decs = dec_mod.load(work)
    trips_root = Path(trips_root).resolve()
    root_dev = trips_root.stat().st_dev
    by_trip: dict[str, TripPlan] = {}
    no_trip: list[str] = []
    kept = []
    for iid, d in sorted(decs.items()):
        if d.get("keep") != "keep" or iid not in items:
            continue
        if not d.get("trip"):
            no_trip.append(iid)
            continue
        by_trip.setdefault(d["trip"], TripPlan(d["trip"]))
        kept.append((iid, d))

    staged_in: dict[str, str] = {}
    for trip, r in _receipts(trips_root).items():
        for iid, entry in r.get("items", {}).items():
            if not _live(entry):
                continue
            staged_in[iid] = trip
            d = decs.get(iid, {})
            if d.get("keep") != "keep" or d.get("trip") != trip:
                now = d.get("keep", "unknown") + (f" → {d['trip']}" if d.get("trip") and d["trip"] != trip else "")
                by_trip.setdefault(trip, TripPlan(trip)).stale.append(
                    f"{iid}: staged here, now {now}; remove {', '.join(entry['dst'])}")

    claimed: dict[Path, Path] = {}
    for iid, d in kept:
        tp, it = by_trip[d["trip"]], items[iid]
        if iid in staged_in and staged_in[iid] != tp.trip:
            tp.problems.append(f"{iid}: still staged in {staged_in[iid]}")
            continue
        if dec_mod.effective(it, d)["changed"]:
            tp.problems.append(f"{iid}: changed since it was reviewed")
            continue
        if "unreadable" in it.get("flags", []):
            tp.problems.append(f"{iid}: unreadable")
            continue
        root = sources.get(it["src"])
        if root is None or not root.is_dir():
            tp.problems.append(f"{iid}: source {it['src']} is missing")
            continue
        ip = ItemPlan(iid, [], [], {})
        for rel in it["files"]:
            s = root / rel
            dst = trips_root / tp.trip / it["src"] / rel
            if s.is_symlink() or not s.is_file():
                tp.problems.append(f"{iid}: {s} is missing or a symlink")
                continue
            sst = s.stat()
            if sst.st_dev != root_dev:
                tp.problems.append(f"{iid}: {s} is on another filesystem than {trips_root}")
                continue
            if claimed.get(dst, s) != s:
                tp.problems.append(f"{iid}: {dst} is claimed by two sources")
                continue
            claimed[dst] = s
            if dst.exists() or dst.is_symlink():
                dstat = dst.lstat()
                if (dstat.st_dev, dstat.st_ino) == (sst.st_dev, sst.st_ino):
                    ip.skips.append(Link(s, dst))
                else:
                    tp.problems.append(f"{iid}: {dst} exists and is a different file")
                continue
            ip.links.append(Link(s, dst))
        both = ip.skips + ip.links
        ip.record = {"src": [str(lk.src) for lk in both], "dst": [str(lk.dst) for lk in both], "fp": it["fp"],
                     "tags": d.get("tags", []), "note": d.get("note", "")}
        tp.items.append(ip)

    if existing_paths is not None:
        for tp in by_trip.values():
            lib = {f"{immich_prefix.rstrip('/')}/{lk.dst.relative_to(trips_root).as_posix()}": lk.dst
                   for ip in tp.items for lk in ip.links}
            for p in sorted(existing_paths(sorted(lib))):
                tp.problems.append(f"{lib[p]} is already an Immich asset ({p})")
    return Plan(sorted(by_trip.values(), key=lambda t: t.trip), sorted(no_trip))


def execute(p: Plan, trips_root: Path) -> int:
    """Link every trip whose plan is clean; returns the number of links made."""
    trips_root = Path(trips_root).resolve()
    made_total = 0
    for tp in p.trips:
        if not tp.ok or not tp.items:
            continue
        rpath = trips_root / tp.trip / RECEIPT
        old = read_json(rpath, {}) or {}
        entries = {k: v for k, v in old.get("items", {}).items() if _live(v)}
        for ip in tp.items:
            made: list[Path] = []
            try:
                for lk in ip.links:
                    lk.dst.parent.mkdir(parents=True, exist_ok=True)
                    os.link(lk.src, lk.dst, follow_symlinks=False)
                    made.append(lk.dst)
            except OSError as e:
                for m in made:
                    m.unlink(missing_ok=True)
                tp.problems.append(f"{ip.id}: {e}")
                break
            made_total += len(made)
            prev = entries.get(ip.id, {})
            entries[ip.id] = {**ip.record, "staged": prev.get("staged") or now_iso()}
        if entries != old.get("items"):
            write_json_atomic(rpath, {"trip": tp.trip, "updated": now_iso(), "items": entries})
    return made_total
