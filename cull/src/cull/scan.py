"""`cull scan`: sources → <work>/_cull/items.json (+ thumbs, proxies, previews).
Sources are only read. Probes are cached by the item's fingerprint, so a
re-scan only looks at new or changed files."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

from . import items as items_mod
from . import media, timeplace
from .core.geo import GeoCache
from .core.jsonio import now_iso, read_json, write_json_atomic

SCHEMA = 1
AGENT = "cull/0.1 (personal media catalogue)"


def cull_dir(work: Path) -> Path:
    return Path(work) / "_cull"


def load_items(work: Path) -> dict:
    doc = read_json(cull_dir(work) / "items.json", None)
    if doc is None:
        raise FileNotFoundError(f"no scan yet in {work}: run `cull scan --work {work} --src DIR`")
    return doc


def media_url(src: str, rel: str) -> str:
    return "/media/" + quote(src, safe="") + "/" + quote(rel, safe="/")


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _str(v) -> str | None:
    return None if v is None else str(v)


def _probe(g: items_mod.Group, root: Path, row: dict | None) -> dict:
    prim = root / g.files[0]
    srt = next((root / f for f in g.files if f.lower().endswith(".srt")), None)
    wall, gps = timeplace.srt_first(srt) if srt else (None, None)
    row = row or {}
    pr = media.ffprobe(prim) if g.kind == "video" else None
    gps = gps or timeplace.exif_gps(row)
    ct = timeplace.capture_time(row, wall, prim.stat().st_mtime, gps)
    flags = []
    if ct.kind == "guess":
        flags.append("time-from-mtime")
    if (g.kind == "video" and pr is None) or (g.kind == "photo" and not row):
        flags.append("unreadable")
    return {"duration": pr.duration if pr else None,
            "width": pr.width if pr else row.get("ImageWidth"),
            "height": pr.height if pr else row.get("ImageHeight"),
            "codec": pr.codec if pr else None,
            "make": _str(row.get("Make")), "model": _str(row.get("Model")),
            "time": ct.time, "time_src": ct.src, "time_kind": ct.kind, "utc": ct.utc, "zone": ct.zone,
            "lat": gps[0] if gps else None, "lon": gps[1] if gps else None, "flags": flags}


def _item(g, root: Path, fp: str, data: dict, cd: Path, geo, proxies: bool, log, n: int, total: int) -> dict:
    files = [root / f for f in g.files]
    h = _h(g.id + fp)
    it = {"id": g.id, "src": g.src, "files": g.files, "kind": g.kind,
          "size": sum(p.stat().st_size for p in files), "primary_size": files[0].stat().st_size, "fp": fp,
          **data, "flags": list(data["flags"]), "place": None, "play": None, "image": None, "thumb": None,
          "immich": []}
    if geo and it["lat"] is not None:
        it["place"] = geo.lookup(it["lat"], it["lon"])
    prim = files[0]
    if g.kind == "video":
        lrf = next((root / f for f in g.files if f.lower().endswith(".lrf")), None)
        if lrf:
            it["play"], thumb_src = media_url(g.src, lrf.relative_to(root).as_posix()), lrf
        elif media.playable(it["codec"], it["width"], it["height"], prim):
            it["play"], thumb_src = media_url(g.src, g.files[0]), prim
        else:
            px = cd / "proxies" / f"{h}.mp4"
            if not px.exists() and proxies and "unreadable" not in it["flags"]:
                log(f"proxy {n}/{total}  {g.id}")
                media.make_proxy(prim, px)
            if px.exists():
                it["play"], thumb_src = f"/work/proxies/{h}.mp4", px
            else:
                it["flags"].append("no-proxy")
                thumb_src = prim
        at = (it["duration"] or 0) * 0.1
    else:
        if prim.suffix.lower() in (".jpg", ".jpeg", ".png"):
            it["image"], thumb_src = media_url(g.src, g.files[0]), prim
        else:
            pv = cd / "previews" / f"{h}.jpg"
            if not pv.exists():
                media.make_preview(prim, pv)
            if pv.exists():
                it["image"], thumb_src = f"/work/previews/{h}.jpg", pv
            else:
                thumb_src = prim
        at = 0.0
    th = cd / "thumbs" / f"{h}.jpg"
    if not th.exists() and "unreadable" not in it["flags"]:
        media.make_thumb(thumb_src, th, at)
    if th.exists():
        it["thumb"] = f"/work/thumbs/{h}.jpg"
    return it


def scan(work: Path, srcs=(), *, immich=None, geocode: bool = True, proxies: bool = True, log=print) -> dict:
    cd = cull_dir(work)
    cd.mkdir(parents=True, exist_ok=True)
    prior = read_json(cd / "items.json", {}) or {}
    sources = items_mod.register_sources(prior.get("sources", {}), [Path(s) for s in srcs])
    if not sources:
        raise ValueError("no sources yet: pass --src DIR")
    groups: list[tuple[items_mod.Group, Path]] = []
    orphans: list[dict] = []
    for name, root in sources.items():
        root = Path(root)
        if not root.is_dir():
            log(f"warning: source {name} is missing: {root} (its decisions are kept)")
            continue
        g, o = items_mod.walk(name, root)
        groups += [(x, root) for x in g]
        orphans += [{"src": name, "rel": r} for r in o]
    cache = read_json(cd / "probe-cache.json", {}) or {}
    fps = {g.id: items_mod.fingerprint(root, g.files) for g, root in groups}
    need = [(g, root) for g, root in groups if (cache.get(g.id) or {}).get("fp") != fps[g.id]]
    rows = timeplace.exif_batch([root / g.files[0] for g, root in need])
    for g, root in need:
        cache[g.id] = {"fp": fps[g.id], "data": _probe(g, root, rows.get(str(root / g.files[0])))}
    present = {g.id for g, _ in groups}
    missing = {n for n, r in sources.items() if not Path(r).is_dir()}
    write_json_atomic(cd / "probe-cache.json",
                      {k: v for k, v in cache.items()
                       if "unreadable" not in v["data"]["flags"]  # re-probed every scan: may be a transient failure
                       and (k in present or k.split("/", 1)[0] in missing)})
    geo = GeoCache(cd / "geocache.json", agent=AGENT) if geocode else None
    out = [_item(g, root, fps[g.id], cache[g.id]["data"], cd, geo, proxies, log, n, len(groups))
           for n, (g, root) in enumerate(groups, 1)]
    if geo:
        geo.flush()
    if immich is not None:
        for it in out:
            try:
                it["immich"] = immich.candidates(it)
            except Exception as e:  # DB dropped mid-scan: keep the scan, skip the hints
                log(f"warning: Immich check failed ({e}); no Immich hints for the rest of this scan")
                break
    doc = {"schema": SCHEMA, "generated": now_iso(), "sources": sources, "items": out, "orphans": orphans}
    write_json_atomic(cd / "items.json", doc)
    return doc
