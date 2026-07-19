"""Triage review web tool — the human half of `immy triage`.

A single-user, tailnet-tunneled Flask app (same shape and discipline as
`dedup.review`): one trip per screen, clips in capture order grouped into
takes, each clip a 6-frame contact sheet from the scan's frame cache, with
K/C/A/T keyboard verdicts written straight to the `triage` table:

    K keep · C compress · A archive (verdict 'cold') · T trash · U undo

This tool NEVER moves, rewrites, or re-encodes a media file. Verdicts are
data; the (future) executor with its own dry-run and quarantine is the only
thing that will act on them, and a verdict it has already applied
(`applied_at` set) can no longer be changed here.

Run via `immy triage review-server` (see cli.py) inside the deploy/n5
container — manifest paths are container paths (/originals/...), and the
frame cache is the scan's --frames-dir (/scratch/triage-frames).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..dedup import manifest
from .engine import is_proxy, map_path, trip_of

VERDICTS = ("keep", "compress", "cold", "trash")

# Containers a browser <video> can realistically play (HEVC needs Safari or
# hw-decode Chrome — both true on faeton's Macs). .insv/.360 never play; the
# UI greys the play button out for those and the endpoint refuses them.
PLAYABLE_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm"}

_FRAME_NAME = re.compile(r"^f\d\.jpg$")


# ------------------------------------------------------------------ format


def human_bytes(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "?"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------------------------------------------------ queries


def load_clips(conn: sqlite3.Connection, root: str) -> list[dict]:
    """Every scanned trip clip + its verdict, in (trip, capture) order.

    One joined query for the whole population (3.8k rows, a few ms) — both
    pages re-derive their rollups from this on every load, so the manifest
    stays the single source of truth and a verdict written by anything else
    shows up on refresh."""
    rows = conn.execute(
        "SELECT a.id, a.path, a.bytes, a.mtime, a.taken_at,"
        "  s.duration_s, s.codec, s.bitrate_kbps, s.take_group,"
        "  s.favorite, s.suggested, s.suggest_reason, s.frames_json,"
        "  t.verdict, t.applied_at"
        "  FROM asset a JOIN video_signal s ON s.asset_id = a.id"
        "  LEFT JOIN triage t ON t.asset_id = a.id"
    ).fetchall()
    clips: list[dict] = []
    for (
        id_, path, bytes_, mtime, taken_at, duration_s, codec, kbps,
        take_group, favorite, suggested, reason, frames_json,
        verdict, applied_at,
    ) in rows:
        trip = trip_of(path, root)
        if trip is None or is_proxy(path):
            continue
        epoch = None
        if taken_at:
            try:
                epoch = datetime.fromisoformat(taken_at).timestamp()
            except ValueError:
                pass
        n_frames = len(json.loads(frames_json)) if frames_json else 0
        clips.append({
            "id": id_, "path": path, "name": Path(path).name, "trip": trip,
            "bytes": bytes_ or 0, "epoch": epoch if epoch is not None else (mtime or 0.0),
            "taken_at": taken_at, "duration_s": duration_s, "codec": codec,
            "bitrate_kbps": kbps, "take_group": take_group,
            "favorite": favorite, "suggested": suggested,
            "suggest_reason": reason, "n_frames": n_frames,
            "playable": Path(path).suffix.lower() in PLAYABLE_SUFFIXES,
            "verdict": verdict, "applied": applied_at is not None,
        })
    clips.sort(key=lambda c: (c["trip"], c["epoch"], c["id"]))
    return clips


def trip_rollup(clips: list[dict]) -> list[dict]:
    """Per-trip progress, biggest undecided bytes first — the index is a
    worklist, so the trip where a review session recovers the most space
    sorts to the top."""
    trips: dict[str, dict] = {}
    for c in clips:
        t = trips.setdefault(c["trip"], {
            "trip": c["trip"], "clips": 0, "bytes": 0,
            "decided": 0, "decided_bytes": 0, "undecided_bytes": 0,
            "by_verdict": {v: 0 for v in VERDICTS},
        })
        t["clips"] += 1
        t["bytes"] += c["bytes"]
        if c["verdict"]:
            t["decided"] += 1
            t["decided_bytes"] += c["bytes"]
            t["by_verdict"][c["verdict"]] += c["bytes"]
        else:
            t["undecided_bytes"] += c["bytes"]
    return sorted(trips.values(), key=lambda t: t["undecided_bytes"], reverse=True)


def group_takes(trip_clips: list[dict]) -> list[list[dict]]:
    """Consecutive same-take_group runs, capture order preserved. Scan
    assigns take_group in capture order per trip, so a plain run-split is
    exact; a NULL take_group (pre-grouping row) becomes its own singleton."""
    groups: list[list[dict]] = []
    for c in trip_clips:
        if (
            groups
            and c["take_group"] is not None
            and groups[-1][0]["take_group"] == c["take_group"]
        ):
            groups[-1].append(c)
        else:
            groups.append([c])
    return groups


# ------------------------------------------------------------------- pages


_CSS = """
:root{--bg:#111;--panel:#181818;--edge:#333;--fg:#eee;--dim:#999;
      --keep:#3f9b46;--compress:#c98a2f;--cold:#4a7dc9;--trash:#c94a4a}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px 20px}
a{color:#7ab7ff;text-decoration:none}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:12px;
       position:sticky;top:0;background:#111e;backdrop-filter:blur(4px);padding:8px 0;z-index:5}
h1{font-size:1.05rem;margin:0}
.progress{color:var(--dim);font-size:.85rem}
.key{display:inline-block;min-width:1.2em;text-align:center;background:#2c2c2c;border:1px solid #444;
     border-radius:4px;padding:0 4px;margin:0 2px;font-family:ui-monospace,monospace;font-size:.75rem}
table.trips{border-collapse:collapse;font-size:.9rem}
table.trips th,table.trips td{border:1px solid #333;padding:8px 14px;text-align:right}
table.trips th{background:#181818}
table.trips td:first-child{text-align:left}
.bar{display:inline-block;height:9px;border-radius:4px;background:#2a2a2a;width:160px;
     overflow:hidden;vertical-align:middle}
.bar i{display:block;height:100%;float:left}
.bar .vkeep{background:var(--keep)}.bar .vcompress{background:var(--compress)}
.bar .vcold{background:var(--cold)}.bar .vtrash{background:var(--trash)}
.take{border:1px solid #2a2a2a;border-left:5px solid #2f4a6f;border-radius:8px;
      margin-bottom:10px;padding:4px 8px;background:#141414}
.take.single{border-left-color:#2a2a2a;background:transparent;border-color:transparent;padding:0}
.takehead{display:flex;gap:10px;align-items:center;font-size:.78rem;color:#9fc0dc;padding:4px 2px}
.takehead button{font-size:.72rem;padding:2px 8px}
.clip{display:flex;gap:10px;align-items:center;border:2px solid transparent;border-radius:8px;
      padding:5px;background:var(--panel);margin:5px 0;scroll-margin:90px}
.clip.focus{border-color:#7ab7ff}
.clip.decided{opacity:.55}
.clip.decided.focus{opacity:.9}
body.hidedecided .clip.decided{display:none}
body.hidedecided .take:not(:has(.clip:not(.decided))){display:none}
.frames{display:flex;gap:2px;flex-shrink:0;cursor:zoom-in}
.frames img{height:96px;width:150px;object-fit:cover;background:#000;border-radius:3px}
.noframes{height:96px;width:300px;display:flex;align-items:center;justify-content:center;color:#666}
.cmeta{font-size:.78rem;line-height:1.5;color:#ccc;min-width:0}
.cmeta .name{font-family:ui-monospace,monospace;font-size:.75rem;color:#aaa;word-break:break-all}
.chip{border-radius:4px;padding:1px 8px;font-size:.73rem;border:1px solid #444;color:#bbb;margin-right:4px}
.chip.suggest{background:#3a2f1d;border-color:#6f5f2f;color:#d0b060}
.chip.fav{background:#4a3a1a;border-color:#7a6a2a;color:#e8d080}
.vchip{font-weight:600;border-radius:4px;padding:2px 10px;font-size:.78rem;margin-left:auto;
       flex-shrink:0;border:1px solid #444;color:#777;min-width:5.5em;text-align:center}
.vchip.keep{background:#1d4620;border-color:var(--keep);color:#9fdca4}
.vchip.compress{background:#46351d;border-color:var(--compress);color:#e8c08a}
.vchip.cold{background:#1d2f46;border-color:var(--cold);color:#9fc0dc}
.vchip.trash{background:#461d1d;border-color:var(--trash);color:#e89f9f}
.vchip.applied{outline:2px dashed #888;cursor:not-allowed}
button{font-size:.85rem;padding:6px 12px;border-radius:8px;border:1px solid #444;background:#222;color:#eee;cursor:pointer}
button:hover{background:#2c2c2c}
button:disabled{opacity:.4;cursor:not-allowed}
button.play{font-size:.72rem;padding:2px 8px}
#lightbox{position:fixed;inset:0;background:#000e;display:none;flex-direction:column;
          align-items:center;justify-content:center;z-index:10;gap:8px}
#lightbox img,#lightbox video{max-width:96vw;max-height:88vh}
#lbnote{color:#999;font-size:.8rem}
#toast{position:fixed;top:14px;right:16px;background:#5a2a2a;border:1px solid #a55;color:#fee;
       padding:8px 14px;border-radius:8px;display:none;z-index:20}
footer{margin-top:18px;color:#666;font-size:.75rem;line-height:1.7}
.filters{margin-left:auto;font-size:.8rem}
.legend{font-size:.75rem;color:#999;line-height:1.9}
.legend b{color:#ccc;font-weight:600}
#help{position:fixed;inset:0;background:#000c;display:none;align-items:center;
      justify-content:center;z-index:15}
#help .card{background:var(--panel);border:1px solid #444;border-radius:12px;
            padding:18px 26px;max-width:560px;font-size:.85rem;line-height:2.1;color:#ccc}
#help h2{font-size:.95rem;margin:0 0 6px}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _verdict_bar(t: dict) -> str:
    total = t["bytes"] or 1
    spans = "".join(
        f"<i class='v{v}' style='width:{100 * t['by_verdict'][v] / total:.1f}%'></i>"
        for v in VERDICTS
    )
    return f"<span class='bar'>{spans}</span>"


def render_index(trips: list[dict]) -> str:
    rows = []
    totals = {
        "clips": 0, "bytes": 0, "decided": 0, "decided_bytes": 0,
        "by_verdict": {v: 0 for v in VERDICTS},
    }
    for t in trips:
        totals["clips"] += t["clips"]
        totals["bytes"] += t["bytes"]
        totals["decided"] += t["decided"]
        totals["decided_bytes"] += t["decided_bytes"]
        for v in VERDICTS:
            totals["by_verdict"][v] += t["by_verdict"][v]
        rows.append(
            f"<tr><td><a href='/trip/{html.escape(t['trip'])}'>"
            f"{html.escape(t['trip'])}</a></td>"
            f"<td>{t['clips']:,}</td><td>{t['bytes'] / 1e9:.1f}</td>"
            f"<td>{t['decided']:,}</td>"
            f"<td>{t['undecided_bytes'] / 1e9:.1f}</td>"
            f"<td>{_verdict_bar(t)}</td></tr>"
        )
    totals_row = (
        f"<tr><th>total</th><th>{totals['clips']:,}</th>"
        f"<th>{totals['bytes'] / 1e9:.1f}</th><th>{totals['decided']:,}</th>"
        f"<th>{(totals['bytes'] - totals['decided_bytes']) / 1e9:.1f}</th>"
        f"<th>{_verdict_bar(totals)}</th></tr>"
    )
    freed = totals["by_verdict"]
    body = f"""
    <header><h1>footage triage</h1>
      <span class="progress">{totals['decided']:,}/{totals['clips']:,} clips decided &middot;
        keep {freed['keep'] / 1e9:.0f}G &middot; compress {freed['compress'] / 1e9:.0f}G &middot;
        cold {freed['cold'] / 1e9:.0f}G &middot; trash {freed['trash'] / 1e9:.0f}G</span>
    </header>
    <table class="trips">
      <tr><th>trip</th><th>clips</th><th>GB</th><th>decided</th>
        <th>undecided GB</th><th>verdicts</th></tr>
      {''.join(rows)}{totals_row}
    </table>
    <footer>Sorted by undecided GB — review top-down for the biggest payoff.
      Open a trip and grade with <span class="key">K</span> keep &middot;
      <span class="key">C</span> compress &middot; <span class="key">A</span> archive &middot;
      <span class="key">T</span> trash (press <span class="key">?</span> inside for the full
      cheat-sheet). Verdicts are data in the manifest; nothing moves or re-encodes
      until the executor runs them. Rough expectations: trash + cold free their
      full size from the vv mirror, compress typically recovers about half.</footer>
    """
    return _page("footage triage", body)


_TRIP_JS = """
const clips = CLIPS;                       // [{id, playable, decided, applied}]
const order = clips.map(c => c.id);
const byId = Object.fromEntries(clips.map(c => [c.id, c]));
let focus = order.findIndex(id => !byId[id].decided);
if (focus < 0) focus = 0;

function el(id) { return document.querySelector('.clip[data-id="' + id + '"]'); }

function setFocus(i, scroll = true) {
  if (i < 0 || i >= order.length) return;
  const prev = el(order[focus]);
  if (prev) prev.classList.remove('focus');
  focus = i;
  const cur = el(order[focus]);
  cur.classList.add('focus');
  if (scroll) cur.scrollIntoView({block: 'nearest', behavior: 'auto'});
}

function nextUndecided(from) {
  for (let i = from + 1; i < order.length; i++)
    if (!byId[order[i]].decided) return i;
  return null;
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.style.display = 'none', 4000);
}

function paint(id) {
  const c = byId[id], row = el(id), chip = row.querySelector('.vchip');
  row.classList.toggle('decided', !!c.decided);
  chip.className = 'vchip' + (c.decided ? ' ' + c.decided : '');
  chip.textContent = c.decided ? ({keep:'keep',compress:'compress',cold:'cold',trash:'trash'})[c.decided] : '—';
  updateHeader();
}

function updateHeader() {
  const done = clips.filter(c => c.decided);
  const gb = v => (clips.filter(c => c.decided === v)
                   .reduce((s, c) => s + c.bytes, 0) / 1e9).toFixed(1);
  document.getElementById('prog').textContent =
    done.length + '/' + clips.length + ' decided \\u00b7 keep ' + gb('keep') +
    'G \\u00b7 compress ' + gb('compress') + 'G \\u00b7 cold ' + gb('cold') +
    'G \\u00b7 trash ' + gb('trash') + 'G';
}

async function verdict(ids, v, reason) {
  ids = ids.filter(id => !byId[id].applied);
  if (!ids.length) { toast('verdict already applied by the executor \\u2014 locked'); return; }
  const res = await fetch('/api/verdict', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({asset_ids: ids, verdict: v, reason: reason || null})});
  const out = await res.json().catch(() => ({}));
  if (!res.ok) { toast('failed: ' + (out.error || res.statusText)); return; }
  ids.forEach(id => { byId[id].decided = v === 'clear' ? null : v; paint(id); });
  const next = nextUndecided(focus);
  if (v !== 'clear' && next !== null) setFocus(next);
}

function groupIds(anyId) {
  const row = el(anyId), take = row.closest('.take');
  return [...take.querySelectorAll('.clip')].map(r => Number(r.dataset.id));
}

// ------------------------------------------------------------- lightbox
let lb = {id: null, frame: 0};
function showFrames(id, frame = 0) {
  const c = byId[id];
  if (!c.frames) { toast('no cached frames for this clip'); return; }
  lb = {id, frame: ((frame % c.frames) + c.frames) % c.frames};
  const box = document.getElementById('lightbox');
  box.querySelector('video').style.display = 'none';
  box.querySelector('video').pause();
  const img = box.querySelector('img');
  img.style.display = 'block';
  img.src = '/frame/' + id + '/f' + lb.frame + '.jpg';
  document.getElementById('lbnote').textContent =
    c.name + ' \\u2014 frame ' + (lb.frame + 1) + '/' + c.frames +
    ' \\u00b7 \\u2190\\u2192/X cycle \\u00b7 ' + (c.playable ? 'P plays video \\u00b7 ' : '') + 'Esc closes';
  box.style.display = 'flex';
}
function playVideo(id) {
  const c = byId[id];
  if (!c.playable) { toast('.' + c.name.split('.').pop() + ' does not play in a browser \\u2014 use the frames'); return; }
  lb.id = id;
  const box = document.getElementById('lightbox');
  box.querySelector('img').style.display = 'none';
  const vid = box.querySelector('video');
  vid.style.display = 'block';
  vid.src = '/video/' + id;
  document.getElementById('lbnote').textContent = c.name + ' \\u00b7 Esc closes';
  box.style.display = 'flex';
  vid.play().catch(() => toast('codec not decodable in this browser \\u2014 use the frames'));
}
function closeLightbox() {
  const box = document.getElementById('lightbox');
  box.querySelector('video').pause();
  box.querySelector('video').removeAttribute('src');
  box.style.display = 'none';
}

// ---------------------------------------------------------------- input
document.addEventListener('click', ev => {
  if (ev.target.closest('#help')) {
    document.getElementById('help').style.display = 'none';
    return;
  }
  if (ev.target.closest('#lightbox')) {
    if (ev.target.tagName !== 'VIDEO') closeLightbox();
    return;
  }
  const gbtn = ev.target.closest('.takehead button');
  if (gbtn) {
    verdict(groupIds(Number(gbtn.closest('.take').querySelector('.clip').dataset.id)),
            gbtn.dataset.v, 'take-group bulk');
    return;
  }
  const playbtn = ev.target.closest('button.play');
  if (playbtn) { playVideo(Number(playbtn.closest('.clip').dataset.id)); return; }
  const row = ev.target.closest('.clip');
  if (row) {
    setFocus(order.indexOf(Number(row.dataset.id)), false);
    if (ev.target.closest('.frames')) {
      const imgs = [...ev.target.closest('.frames').querySelectorAll('img')];
      showFrames(Number(row.dataset.id), Math.max(0, imgs.indexOf(ev.target)));
    }
  }
});

const KEYMAP = {k: 'keep', c: 'compress', a: 'cold', t: 'trash'};
document.addEventListener('keydown', ev => {
  if (ev.target.tagName === 'INPUT' || ev.metaKey || ev.ctrlKey) return;
  const help = document.getElementById('help');
  if (ev.key === '?' || (help.style.display === 'flex' && ev.key === 'Escape')) {
    help.style.display = help.style.display === 'flex' ? 'none' : 'flex';
    ev.preventDefault();
    return;
  }
  if (help.style.display === 'flex') return;   // modal: swallow other keys
  const box = document.getElementById('lightbox');
  const inLb = box.style.display === 'flex';
  const key = ev.key.toLowerCase();
  if (inLb) {
    if (ev.key === 'Escape' || ev.key === 'Enter') { closeLightbox(); ev.preventDefault(); return; }
    if (key === 'x' || ev.key === 'ArrowRight') { showFrames(lb.id, lb.frame + 1); ev.preventDefault(); return; }
    if (ev.key === 'ArrowLeft') { showFrames(lb.id, lb.frame - 1); ev.preventDefault(); return; }
    if (key === 'p') { playVideo(lb.id); ev.preventDefault(); return; }
    // verdict keys fall through — grade straight from the lightbox
  }
  const id = order[focus];
  if (KEYMAP[key]) {
    verdict(ev.shiftKey ? groupIds(id) : [id], KEYMAP[key],
            ev.shiftKey ? 'take-group bulk' : null);
    ev.preventDefault();
  } else if (key === 'u') {
    verdict(ev.shiftKey ? groupIds(id) : [id], 'clear');
    ev.preventDefault();
  } else if (ev.key === 'ArrowDown' || key === 'j') { setFocus(focus + 1); ev.preventDefault(); }
  else if (ev.key === 'ArrowUp') { setFocus(focus - 1); ev.preventDefault(); }
  else if (key === 'z' || ev.key === ' ') { showFrames(id); ev.preventDefault(); }
  else if (key === 'p') { playVideo(id); ev.preventDefault(); }
  else if (key === 'h') {
    document.body.classList.toggle('hidedecided');
    document.getElementById('hidebox').checked = document.body.classList.contains('hidedecided');
    ev.preventDefault();
  }
});
document.getElementById('hidebox').addEventListener('change', ev =>
  document.body.classList.toggle('hidedecided', ev.target.checked));

setFocus(focus);
updateHeader();
"""


def render_trip(trip: str, groups: list[list[dict]]) -> str:
    take_blocks: list[str] = []
    js_clips: list[dict] = []
    for group in groups:
        rows: list[str] = []
        for c in group:
            js_clips.append({
                "id": c["id"], "name": c["name"], "bytes": c["bytes"],
                "frames": c["n_frames"], "playable": c["playable"],
                "decided": c["verdict"], "applied": c["applied"],
            })
            frames = (
                "".join(
                    f"<img src='/frame/{c['id']}/f{i}.jpg' loading='lazy'>"
                    for i in range(c["n_frames"])
                )
                if c["n_frames"]
                else "<div class='noframes'>no frames cached</div>"
            )
            mbps = (
                f"{c['bitrate_kbps'] / 1000:.0f} Mbps"
                if c["bitrate_kbps"] else "?"
            )
            suggest_chip = (
                f"<span class='chip suggest' title='{html.escape(c['suggest_reason'] or '')}'>"
                f"suggest: {html.escape(c['suggested'])}</span>"
                if c["suggested"] else ""
            )
            fav_chip = "<span class='chip fav'>&#9733; favorite</span>" if c["favorite"] else ""
            play = (
                "<button class='play'>&#9654; play</button>"
                if c["playable"] else ""
            )
            verdict_class = f" {c['verdict']}" if c["verdict"] else ""
            verdict_label = c["verdict"] or "&mdash;"
            applied = " applied" if c["applied"] else ""
            decided = " decided" if c["verdict"] else ""
            rows.append(f"""
            <div class="clip{decided}" data-id="{c['id']}">
              <div class="frames">{frames}</div>
              <div class="cmeta">
                <div class="name">{html.escape(c['name'])}</div>
                {fmt_duration(c['duration_s'])} &middot; {human_bytes(c['bytes'])}
                &middot; {mbps} {html.escape(c['codec'] or '?')}
                &middot; {html.escape((c['taken_at'] or '?')[:16])}<br>
                {suggest_chip}{fav_chip}{play}
              </div>
              <div class="vchip{verdict_class}{applied}">{verdict_label}</div>
            </div>""")
        if len(group) >= 2:
            size = sum(c["bytes"] for c in group)
            buttons = "".join(
                f"<button data-v='{v}'>{label} all</button>"
                for v, label in (
                    ("keep", "keep"), ("compress", "compress"),
                    ("cold", "cold"), ("trash", "trash"),
                )
            )
            take_blocks.append(
                f"<div class='take'><div class='takehead'>"
                f"&#127916; take &middot; {len(group)} clips &middot; "
                f"{human_bytes(size)} &middot; same scene, pick the best"
                f"<span style='margin-left:auto'></span>{buttons}"
                f"</div>{''.join(rows)}</div>"
            )
        else:
            take_blocks.append(f"<div class='take single'>{''.join(rows)}</div>")

    body = f"""
    <header>
      <h1><a href="/">&larr;</a> {html.escape(trip)}</h1>
      <span class="progress" id="prog"></span>
      <span class="filters"><label><input type="checkbox" id="hidebox">
        hide decided <span class="key">H</span></label></span>
      <span class="legend" style="width:100%">
        <span class="key">K</span><b>keep</b> &middot;
        <span class="key">C</span><b>compress</b> &middot;
        <span class="key">A</span><b>archive</b> &middot;
        <span class="key">T</span><b>trash</b> &middot;
        <span class="key">U</span>undo &middot;
        <span class="key">&#8679;</span>+key = whole take &middot;
        <span class="key">&darr;</span><span class="key">&uarr;</span> move &middot;
        <span class="key">Z</span> zoom &middot;
        <span class="key">P</span> play &middot;
        <span class="key">?</span> help
      </span>
    </header>
    <div id="clips">{''.join(take_blocks)}</div>
    <footer>
      <span class="key">K</span> keep &middot; <span class="key">C</span> compress &middot;
      <span class="key">A</span> archive to cold &middot; <span class="key">T</span> trash &middot;
      <span class="key">U</span> undo &middot; shift+key = whole take &middot;
      <span class="key">&darr;</span>/<span class="key">J</span>/<span class="key">&uarr;</span> move &middot;
      <span class="key">Z</span>/space or click frames = zoom (<span class="key">X</span>/&larr;&rarr; cycles) &middot;
      <span class="key">P</span> play video &middot; <span class="key">H</span> hide decided.
      Verdict keys work inside the zoom too. Verdicts write to the manifest only —
      the executor moves/encodes later; dashed outline = already applied, locked.
    </footer>
    <div id="lightbox"><img><video controls></video><div id="lbnote"></div></div>
    <div id="help"><div class="card">
      <h2>grading keys</h2>
      <span class="key">K</span> keep &middot; <span class="key">C</span> compress (re-encode later) &middot;
      <span class="key">A</span> archive to cold storage &middot; <span class="key">T</span> trash &middot;
      <span class="key">U</span> undo a verdict<br>
      <span class="key">&#8679;</span>+any of those applies it to the whole take block<br>
      <h2>moving around</h2>
      <span class="key">&darr;</span>/<span class="key">J</span> next clip &middot;
      <span class="key">&uarr;</span> previous &middot; click a clip to focus it &middot;
      <span class="key">H</span> hide decided clips<br>
      <h2>looking closer</h2>
      <span class="key">Z</span>/space or click a frame = zoom &middot;
      in zoom <span class="key">X</span>/<span class="key">&larr;</span><span class="key">&rarr;</span>
      cycle frames, verdict keys still work &middot;
      <span class="key">P</span> plays mp4/mov in the browser (.insv can't play — use the frames)<br>
      <h2>what a verdict does</h2>
      Writes a row in the manifest, nothing more — no file is moved or
      re-encoded until the executor runs, and you can re-grade or
      <span class="key">U</span>ndo any time before that.<br>
      <span class="key">?</span>/<span class="key">Esc</span> closes this.
    </div></div>
    <div id="toast"></div>
    <script>const CLIPS = {json.dumps(js_clips)};\n{_TRIP_JS}</script>
    """
    return _page(f"triage — {trip}", body)


# --------------------------------------------------------------------- app


def create_app(
    manifest_path: Path, frames_root: Path, root: str, fs_root: str | None
):
    """Flask app factory — one sqlite connection per request, short writes,
    same WAL discipline as the dedup review server."""
    from flask import Flask, abort, jsonify, request, send_file

    app = Flask("immy-triage-review")
    fs_root = fs_root or root

    def db() -> sqlite3.Connection:
        return manifest.open_manifest(manifest_path)

    @app.get("/")
    def index():
        conn = db()
        try:
            return render_index(trip_rollup(load_clips(conn, root)))
        finally:
            conn.close()

    @app.get("/trip/<trip>")
    def trip_page(trip: str):
        conn = db()
        try:
            clips = [c for c in load_clips(conn, root) if c["trip"] == trip]
        finally:
            conn.close()
        if not clips:
            abort(404)
        return render_trip(trip, group_takes(clips))

    @app.post("/api/verdict")
    def set_verdict():
        payload = request.get_json(force=True, silent=True) or {}
        verdict = payload.get("verdict")
        if verdict not in VERDICTS + ("clear",):
            return jsonify(error=f"unknown verdict {verdict!r}"), 400
        try:
            ids = [int(i) for i in payload.get("asset_ids") or []]
        except (TypeError, ValueError):
            return jsonify(error="asset_ids must be integers"), 400
        if not ids:
            return jsonify(error="asset_ids required"), 400
        reason = payload.get("reason") or None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = db()
        try:
            marks = ",".join("?" * len(ids))
            locked = [
                r[0] for r in conn.execute(
                    f"SELECT asset_id FROM triage WHERE asset_id IN ({marks})"
                    " AND applied_at IS NOT NULL", ids,
                )
            ]
            if locked:
                # The executor already acted on these — a changed verdict
                # here would silently disagree with what's on disk.
                return jsonify(
                    error=f"verdict already applied for assets {locked}; "
                    "re-triage them with the executor's tools, not the UI"
                ), 409
            known = {
                r[0] for r in conn.execute(
                    f"SELECT asset_id FROM video_signal WHERE asset_id IN ({marks})",
                    ids,
                )
            }
            unknown = [i for i in ids if i not in known]
            if unknown:
                return jsonify(error=f"assets {unknown} were never scanned"), 400
            if verdict == "clear":
                conn.execute(
                    f"DELETE FROM triage WHERE asset_id IN ({marks})", ids
                )
            else:
                conn.executemany(
                    "INSERT INTO triage (asset_id, verdict, reason, decided_by,"
                    "  decided_at) VALUES (?, ?, ?, 'human', ?)"
                    " ON CONFLICT(asset_id) DO UPDATE SET"
                    "  verdict=excluded.verdict, reason=excluded.reason,"
                    "  decided_by='human', decided_at=excluded.decided_at",
                    [(i, verdict, reason, now) for i in ids],
                )
            conn.commit()
            return jsonify(ok=True, verdict=verdict, count=len(ids))
        finally:
            conn.close()

    @app.get("/frame/<int:asset_id>/<name>")
    def frame(asset_id: int, name: str):
        if not _FRAME_NAME.match(name):
            abort(404)
        path = frames_root / str(asset_id) / name
        if not path.exists():
            abort(404)
        return send_file(path, mimetype="image/jpeg", max_age=86400)

    @app.get("/video/<int:asset_id>")
    def video(asset_id: int):
        conn = db()
        try:
            row = conn.execute(
                "SELECT path FROM asset WHERE id=?", (asset_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        suffix = Path(row[0]).suffix.lower()
        if suffix not in PLAYABLE_SUFFIXES:
            abort(415)
        src = map_path(row[0], root, fs_root)
        if not src.exists():
            abort(404)
        mimetype = "video/quicktime" if suffix == ".mov" else "video/mp4"
        # conditional=True → Range support, so seeking works and the browser
        # never pulls a whole 8 GB master to show the first second.
        return send_file(src, mimetype=mimetype, conditional=True)

    return app


def serve(
    manifest_path: Path, frames_root: Path, root: str, fs_root: str | None,
    host: str, port: int,
) -> None:
    app = create_app(manifest_path, frames_root, root, fs_root)
    app.run(host=host, port=port, threaded=True)
