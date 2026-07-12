"""Manual review web tool for `immy dedup` review clusters.

A single-user, localhost-tunneled Flask app that walks the human through
every cluster Stage D left at decision='review' (safest-first: highest
clip_cos_sim), one cluster per screen, and records the outcome in
manifest.sqlite in EXACTLY the shape `decide()` writes (via
`engine.commit_cluster_decision`, the shared write path):

    merge (pick winner)  -> decision='auto'  + winner  -> `dedup apply`
                            promotes/quarantines it on its next run
    keep all             -> decision='kept_all'        -> never touched
    skip                 -> no write at all            -> stays in queue

This tool NEVER moves, copies, or deletes a media file. `dedup apply`
(unmodified) remains the only file-mover.

Scope (v1): clusters with a clip_cos_sim only — i.e. image clusters that
went through Stage C. Video-only review clusters (no CLIP score, no pHash)
are a v2 extension and are surfaced as a count in the footer, nothing more.

Run via `immy dedup review-server` (see cli.py) inside the immy container —
asset paths in the manifest are container paths (/staging/..., /originals/...),
so thumbnailing only works with the container's mounts.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path

from . import manifest, phash, signals
from .engine import (
    HAMMING_STRONG,
    AssetLite,
    _aspect_change,
    _confidence,
    _metadata_agrees,
    commit_cluster_decision,
    load_cluster_members,
    winner_score,
)

THUMB_SIZE = 640        # card tier — the static gallery's 240px was too small
LIGHTBOX_SIZE = 1600    # click-to-enlarge tier
BATCH_SIZE = 60         # clusters per /batch page — one glance-and-Enter unit

# exiftool embedded-preview tags, in preference order (RAW/corrupt fallback).
PREVIEW_TAGS = ["-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"]


# -------------------------------------------------------------- thumbnails


def make_thumb(src_path: str, dest_jpg: Path, max_dim: int) -> bool:
    """Copied from scratch/gen_triage.py (parametrized size), because that
    version already solved a real bug — do not reorder the strategies.

    Direct decode first — libheif-backed libvips handles HEIC/JPG/PNG (and
    sometimes RAW) directly, no extraction needed. Diagnosed 2026-07-12:
    routing HEIC through exiftool preview-extraction first was wrong — most
    plain (non-Live-Photo) iPhone HEIC files have no extractable embedded
    preview blob at all, causing an ~84% failure rate on HEIC even though
    direct pyvips decode succeeds instantly. exiftool stays as the fallback
    for RAW formats libvips can't decode (sensor data, not a standard image
    container) and truly corrupt files.

    Writes via a tmp file + atomic replace so a concurrent request or a
    crash mid-write can never leave a truncated jpeg in the cache.
    """
    import pyvips

    tmp = dest_jpg.with_name(f".{dest_jpg.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        img = pyvips.Image.thumbnail(src_path, max_dim)
        img.jpegsave(str(tmp), Q=80)
        if tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, dest_jpg)
            return True
    except Exception:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    for tag in PREVIEW_TAGS:
        try:
            raw = subprocess.run(
                ["exiftool", "-b", tag, src_path],
                capture_output=True, timeout=30,
            ).stdout
            if not raw or len(raw) < 500:
                continue
            img = pyvips.Image.thumbnail_buffer(raw, max_dim)
            img.jpegsave(str(tmp), Q=80)
            if tmp.exists() and tmp.stat().st_size > 0:
                os.replace(tmp, dest_jpg)
                return True
        except Exception:
            continue
        finally:
            tmp.unlink(missing_ok=True)
    return False


def human_bytes(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ------------------------------------------------------------------ queries


def _queue_ids(conn: sqlite3.Connection) -> list[int]:
    """The reviewable population, safest-first (same sort as the static
    gallery): image clusters with a Stage C score. Decided clusters drop out
    automatically — the manifest itself is the queue state."""
    return [
        r[0] for r in conn.execute(
            "SELECT id FROM cluster WHERE decision='review'"
            " AND clip_cos_sim IS NOT NULL"
            " ORDER BY clip_cos_sim DESC, id ASC"
        )
    ]


def _load_all_pending(
    conn: sqlite3.Connection, skipped: set[int]
) -> list[tuple[int, list[AssetLite], float | None]]:
    """(cluster_id, members, pixel_ncc) for every pending queue cluster, in
    queue order, members fetched in ONE query — the categories page and
    filtered batch views classify the whole queue on each load (1-2k
    clusters, a few ms of sqlite + pure-python grouping)."""
    order = {cid: i for i, cid in enumerate(_queue_ids(conn))}
    by_cluster: dict[int, list[AssetLite]] = {}
    from .engine import ASSET_LITE_COLUMNS, asset_lite_from_row

    # cluster.id makes bare `id` ambiguous in this 3-table join — qualify
    # every asset column.
    qualified = ", ".join(
        f"asset.{column.strip()}" for column in ASSET_LITE_COLUMNS.split(",")
    )
    for row in conn.execute(
        f"SELECT m.cluster_id, {qualified}"
        "  FROM asset JOIN membership m ON m.asset_id = asset.id"
        "  JOIN cluster c ON c.id = m.cluster_id"
        " WHERE c.decision='review' AND c.clip_cos_sim IS NOT NULL"
        " ORDER BY asset.id"
    ):
        by_cluster.setdefault(row[0], []).append(asset_lite_from_row(row[1:]))
    try:
        px = dict(conn.execute("SELECT cluster_id, pixel_ncc FROM review_signal"))
    except sqlite3.OperationalError:
        px = {}
    return [
        (cid, members, px.get(cid))
        for cid, members in sorted(by_cluster.items(), key=lambda kv: order.get(kv[0], 1 << 30))
        if cid not in skipped and len(members) >= 2
    ]


def _counts(conn: sqlite3.Connection) -> dict:
    remaining = conn.execute(
        "SELECT COUNT(*) FROM cluster WHERE decision='review' AND clip_cos_sim IS NOT NULL"
    ).fetchone()[0]
    no_clip = conn.execute(
        "SELECT COUNT(*) FROM cluster WHERE decision='review' AND clip_cos_sim IS NULL"
    ).fetchone()[0]
    return {"remaining": remaining, "no_clip": no_clip}


def validate_merge(
    conn: sqlite3.Connection, cluster_id: int, winner_id: int
) -> tuple[tuple[int, str] | None, list[AssetLite]]:
    """Shared validation for single and batch merges. Returns
    ((http_status, error), []) on rejection, or (None, members) when the
    merge is safe to commit."""
    row = conn.execute(
        "SELECT decision FROM cluster WHERE id=?", (cluster_id,)
    ).fetchone()
    if row is None:
        return (404, f"no cluster {cluster_id}"), []
    if row[0] != "review":
        # Stale tab / double-submit — never silently overwrite a decision
        # that is no longer 'review'.
        return (409, f"cluster {cluster_id} is already '{row[0]}'"), []
    members = load_cluster_members(conn, cluster_id)
    by_id = {m.id: m for m in members}
    if winner_id not in by_id:
        return (400, f"asset {winner_id} is not a member of cluster {cluster_id}"), []
    if (
        any(m.source == "originals" for m in members)
        and by_id[winner_id].source != "originals"
    ):
        return (
            400,
            "cluster contains a library/originals member; the winner must be "
            "the originals copy (a different winner would make `dedup apply` "
            "promote a second copy next to the canonical file)",
        ), []
    return None, members


def review_reason(members: list[AssetLite]) -> str:
    """Best-effort, purely informational: which of Stage D's checks routed
    this cluster to review (mirrors _decide_one's guard order). Tells the
    reviewer what to look FOR — a crop, a different shot, an edit — instead
    of making them guess."""
    if any(m.burst_uuid for m in members):
        return "burst group"
    if any(m.edited for m in members) and not all(m.edited for m in members):
        return "edited + unedited mixed"
    winner = max(members, key=winner_score)
    reasons: list[str] = []
    for m in members:
        if m.id == winner.id:
            continue
        if _aspect_change(winner, m) > 0.05:
            reason = "aspect/crop differs"
        elif m.source == "originals":
            reason = "would displace originals"
        elif m.media_type == "image" and (
            winner.phash is None or m.phash is None
            or phash.hamming(winner.phash, m.phash) > HAMMING_STRONG
        ):
            reason = "pHash weak — check: same shot or near-duplicate?"
        elif not _metadata_agrees(winner, m):
            reason = "metadata disagrees"
        else:
            continue
        if reason not in reasons:
            reasons.append(reason)
    return ", ".join(reasons) or "borderline auto criteria"


def default_winner(members: list[AssetLite]) -> AssetLite:
    """Pre-selected keeper: same heuristic decide() uses — except when the
    cluster contains a library/originals member, which is pre-locked as the
    winner (promoting a different member over an already-canonical file is a
    swap, not a promote — see _decide_one's originals guard)."""
    originals = [m for m in members if m.source == "originals"]
    pool = originals or members
    return max(pool, key=winner_score)


def reason_slug(members: list[AssetLite]) -> str:
    """Primary category key derived from review_reason (first guard hit)."""
    reason = review_reason(members)
    for needle, slug in (
        ("burst", "burst"), ("edited", "edited"), ("aspect", "aspect-crop"),
        ("originals", "originals"), ("pHash weak", "phash-weak"),
        ("metadata", "metadata"),
    ):
        if needle in reason:
            return slug
    return "other"


# (label, min_px, max_px) — max exclusive; None = open-ended
PX_BANDS = [
    ("likely same frame", 0.90, None),
    ("ambiguous upper", 0.85, 0.90),
    ("ambiguous lower", 0.80, 0.85),
    ("lean distinct", 0.75, 0.80),
    ("likely distinct", None, 0.75),
]


def _band_of(px: float | None) -> int | None:
    if px is None:
        return None
    for i, (_, lo, hi) in enumerate(PX_BANDS):
        if (lo is None or px >= lo) and (hi is None or px < hi):
            return i
    return None


def pixel_chip(pixel_ncc: float | None, time_delta: float | None) -> str:
    """Signal chip for the `dedup rescore` scores: green = pixels correlate
    like a same-frame re-export, blue = they diverge like distinct shots."""
    if pixel_ncc is None:
        return ""
    kind = "same" if pixel_ncc >= 0.97 else ("diff" if pixel_ncc <= 0.85 else "")
    delta = f" &middot; &Delta;t {time_delta:.0f}s" if time_delta is not None else ""
    return f"<span class='pixel {kind}'>pixel {pixel_ncc:.3f}{delta}</span>"


# ------------------------------------------------------------------- pages


_CSS = """
:root{--bg:#111;--panel:#181818;--edge:#333;--fg:#eee;--dim:#999}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px 20px}
a{color:#7ab7ff}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:1.05rem;margin:0}
.progress{color:var(--dim);font-size:.85rem}
.cards{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}
.card{background:var(--panel);border:2px solid var(--edge);border-radius:10px;overflow:hidden;cursor:pointer;
      width:min(46vw, 660px);transition:border-color .1s}
.card.selected{border-color:#4caf50;box-shadow:0 0 0 2px #4caf5055}
.card.locked{cursor:not-allowed}
.card img{display:block;width:100%;max-height:70vh;object-fit:contain;background:#000}
.noimg{height:200px;display:flex;align-items:center;justify-content:center;color:#666}
.meta{padding:8px 10px;font-size:.78rem;line-height:1.45;color:#ccc}
.key{display:inline-block;min-width:1.2em;text-align:center;background:#2c2c2c;border:1px solid #444;
     border-radius:4px;padding:0 4px;margin-right:6px;font-family:ui-monospace,monospace;font-size:.75rem}
.src{font-weight:600;padding:1px 6px;border-radius:3px;margin-right:6px}
.src.icloud{background:#2a4d2a}.src.google{background:#2a3a5a}.src.gdrive{background:#5a3a2a}.src.originals{background:#5a2a4d}
.winnertag{color:#4caf50;font-weight:600;margin-left:6px}
.locknote{color:#f0ad4e;font-size:.78rem;margin:8px 0}
.reason{background:#3a2f1d;border:1px solid #6f5f2f;border-radius:4px;padding:1px 8px;
        font-size:.75rem;color:#d0b060}
.ham{color:#c9a0d0}
.pixel{border-radius:4px;padding:1px 8px;font-size:.75rem;border:1px solid #444;color:#bbb}
.pixel.same{background:#1d4620;border-color:#2f6f34;color:#9fdca4}
.pixel.diff{background:#1d2f46;border-color:#2f4a6f;color:#9fc0dc}
table.cat{border-collapse:collapse;margin-top:10px;font-size:.9rem}
table.cat th,table.cat td{border:1px solid #333;padding:8px 14px;text-align:right}
table.cat th{background:#181818;text-align:left}
table.cat td a{font-weight:600}
table.cat small{color:#777;font-weight:400}
.filternote{background:#1d2f46;border:1px solid #2f4a6f;border-radius:4px;
            padding:1px 8px;font-size:.78rem;color:#9fc0dc}
.path{color:#888;word-break:break-all;font-size:.72rem;margin-top:3px}
.score{color:var(--dim)}
.actions{position:sticky;bottom:0;background:#111d;backdrop-filter:blur(4px);padding:12px 0;margin-top:14px;
         display:flex;gap:10px;flex-wrap:wrap}
button{font-size:.9rem;padding:9px 16px;border-radius:8px;border:1px solid #444;background:#222;color:#eee;cursor:pointer}
button:hover{background:#2c2c2c}
button.merge{background:#1d4620;border-color:#2f6f34}
button.merge:hover{background:#245a28}
button:disabled{opacity:.45;cursor:not-allowed}
#lightbox{position:fixed;inset:0;background:#000d;display:none;align-items:center;justify-content:center;z-index:10}
#lightbox img{max-width:96vw;max-height:96vh}
#toast{position:fixed;top:14px;right:16px;background:#5a2a2a;border:1px solid #a55;color:#fee;
       padding:8px 14px;border-radius:8px;display:none;z-index:20}
footer{margin-top:18px;color:#666;font-size:.75rem}
.done{margin-top:20vh;text-align:center;color:var(--dim);font-size:1.1rem}
.modeswitch{font-size:.85rem}
.brow{display:flex;gap:8px;align-items:center;border:2px solid #2f6f34;border-radius:8px;
      padding:6px;margin-bottom:8px;background:var(--panel);cursor:pointer}
.brow.unchecked{border-color:#333;opacity:.45}
.brow img{height:320px;max-width:480px;object-fit:contain;background:#000;border-radius:4px}
.thresh{display:flex;gap:8px;align-items:center;margin-left:auto}
.thresh input{width:6.5em;font-size:.9rem;padding:8px;border-radius:8px;border:1px solid #444;
              background:#1a1a1a;color:#eee}
button.danger{background:#4a1d1d;border-color:#6f2f2f}
button.danger:hover{background:#5a2424}
.brow .keepmark{outline:3px solid #4caf50;outline-offset:-3px}
.brow .bmeta{font-size:.72rem;color:#aaa;min-width:130px}
.brow .check{font-size:1.3rem;width:1.4em;text-align:center;color:#4caf50}
.brow.unchecked .check{color:#555}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


_JS = """
const state = {selected: DEFAULT_WINNER, locked: LOCKED, clusterId: CLUSTER_ID, members: MEMBERS};

function select(assetId) {
  if (state.locked && assetId !== state.selected) {
    toast('winner locked: this cluster contains a library/originals member — merging must keep the originals copy');
    return;
  }
  state.selected = assetId;
  document.querySelectorAll('.card').forEach(c =>
    c.classList.toggle('selected', Number(c.dataset.asset) === assetId));
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.style.display = 'none', 4000);
}

let inflight = false;
async function post(url, payload) {
  if (inflight) return;
  inflight = true;
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify(payload || {})});
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || (res.status + ' ' + res.statusText));
    }
    window.location.href = '/';
  } catch (e) {
    toast('failed: ' + e.message);
    inflight = false;
    document.querySelectorAll('button').forEach(b => b.disabled = false);
  }
}

const merge   = () => post('/api/decide/' + state.clusterId,
                           {action: 'merge', winner_asset_id: state.selected});
const keepAll = () => post('/api/decide/' + state.clusterId, {action: 'keep_all'});
const skip    = () => post('/api/skip/'   + state.clusterId);

let lightboxIndex = 0;
function lightbox(assetId) {
  const box = document.getElementById('lightbox');
  lightboxIndex = Math.max(0, state.members.indexOf(assetId));
  box.querySelector('img').src = '/thumb/' + assetId + '?size=large';
  box.style.display = 'flex';
}
// Flicker-compare: same frame -> the image "blinks" in place; a different
// shot -> everything jumps. Far faster than side-by-side scanning.
function lightboxCycle() {
  lightboxIndex = (lightboxIndex + 1) % state.members.length;
  document.querySelector('#lightbox img').src =
    '/thumb/' + state.members[lightboxIndex] + '?size=large';
}
document.addEventListener('click', ev => {
  const card = ev.target.closest('.card');
  if (card) {
    if (ev.target.tagName === 'IMG' && ev.altKey) { lightbox(Number(card.dataset.asset)); return; }
    select(Number(card.dataset.asset));
  }
});
document.getElementById('lightbox').addEventListener('click',
  ev => ev.currentTarget.style.display = 'none');

document.addEventListener('keydown', ev => {
  if (ev.target.tagName === 'INPUT') return;
  const box = document.getElementById('lightbox');
  if (box.style.display === 'flex') {
    if (ev.key === 'Escape' || ev.key === 'Enter') {
      box.style.display = 'none'; ev.preventDefault(); return;
    }
    if (ev.key === 'x' || ev.key === 'X' || ev.key === ' ') {
      lightboxCycle(); ev.preventDefault(); return;
    }
  }
  if (ev.key >= '1' && ev.key <= '9') {
    const i = Number(ev.key) - 1;
    if (i < state.members.length) select(state.members[i]);
  } else if (ev.key === 'Enter') { merge(); ev.preventDefault(); }
  else if (ev.key === 'k' || ev.key === 'K') keepAll();
  else if (ev.key === 's' || ev.key === 'S' || ev.key === 'ArrowRight') skip();
  else if (ev.key === 'z' || ev.key === 'Z' || ev.key === ' ') {
    lightbox(state.selected); ev.preventDefault();
  }
});

// Warm the browser cache with the next clusters' thumbnails so the page
// after a decision renders instantly.
PREFETCH.forEach(id => { const img = new Image(); img.src = '/thumb/' + id; });
"""


def render_cluster(
    cluster_id: int,
    clip_cos_sim: float | None,
    members: list[AssetLite],
    counts: dict,
    prefetch_ids: tuple[int, ...] = (),
    signal: tuple[float | None, float | None] = (None, None),
) -> str:
    suggested = default_winner(members)
    locked = any(m.source == "originals" for m in members)

    cards = []
    for i, m in enumerate(members):
        key_hint = f"<span class='key'>{i + 1}</span>" if i < 9 else ""
        ham_note = ""
        if (
            m.id != suggested.id
            and m.phash is not None and suggested.phash is not None
        ):
            distance = phash.hamming(m.phash, suggested.phash)
            ham_note = (
                f" &middot; <span class='ham'>pHash &Delta;{distance} vs keeper"
                f"{' (same pixels)' if distance <= HAMMING_STRONG else ''}</span>"
            )
        winner_note = (
            "<span class='winnertag'>&#10003; recommended keeper</span>"
            if m.id == suggested.id else ""
        )
        # The recommended keeper renders pre-selected — Enter merges it with
        # zero extra clicks (the whole point of the suggestion).
        classes = "card"
        if m.id == suggested.id:
            classes += " selected"
        elif locked:
            classes += " locked"
        cards.append(f"""
        <div class="{classes}" data-asset="{m.id}">
          <img src="/thumb/{m.id}" loading="lazy"
               onerror="this.outerHTML='<div class=noimg>no preview</div>'">
          <div class="meta">
            {key_hint}<span class="src {html.escape(m.source)}">{html.escape(m.source)}</span>
            {m.width or '?'}x{m.height or '?'} &middot; {human_bytes(m.bytes)}
            &middot; {html.escape(m.format or '?')}
            &middot; {html.escape(m.taken_at or '?')} ({html.escape(m.taken_src or '?')})
            &middot; <span class="score">score {winner_score(m):.0f}</span>{ham_note}
            {winner_note}
            <div class="path">{html.escape(m.path)}</div>
          </div>
        </div>""")

    lock_note = (
        "<p class='locknote'>&#9888; contains a library/originals member — "
        "winner is locked to the originals copy (replacing a canonical file "
        "is a swap, not a promote; that stays a manual job)</p>"
        if locked else ""
    )
    # 6 decimals: a "1.0000" that is really 0.99996 is exactly the kind of
    # doubt the reviewer needs resolved.
    sim = f"{clip_cos_sim:.6f}" if clip_cos_sim is not None else "—"
    config = (
        f"const CLUSTER_ID = {cluster_id};"
        f" const DEFAULT_WINNER = {suggested.id};"
        f" const LOCKED = {json.dumps(locked)};"
        f" const MEMBERS = {json.dumps([m.id for m in members])};"
        f" const PREFETCH = {json.dumps(list(prefetch_ids))};"
    )
    body = f"""
    <header>
      <h1>cluster {cluster_id}</h1>
      <span class="progress">clip_cos_sim {sim} &middot; {len(members)} members
        &middot; {counts['remaining']:,} remaining</span>
      <span class="reason" title="which Stage D check routed this cluster to review">
        {html.escape(review_reason(members))}</span>
      {pixel_chip(*signal)}
      <span class="modeswitch"><a href="/categories">categories</a> &middot;
        <a href="/batch">batch mode &rarr;</a></span>
    </header>
    {lock_note}
    <div class="cards">{''.join(cards)}</div>
    <div class="actions">
      <button class="merge" onclick="merge()">Duplicates &mdash; keep selected <span class="key">&#9166;</span></button>
      <button onclick="keepAll()">Not duplicates &mdash; keep all <span class="key">K</span></button>
      <button onclick="skip()">Skip <span class="key">S</span></button>
      <button onclick="lightbox(state.selected)">Zoom selected <span class="key">Z</span></button>
    </div>
    <footer>
      <span class="key">1</span>-<span class="key">9</span> select keeper &middot;
      <span class="key">&#9166;</span> merge &middot; <span class="key">K</span> keep all &middot;
      <span class="key">S</span>/<span class="key">&rarr;</span> skip &middot;
      <span class="key">Z</span>/space or alt-click zoom &middot;
      <span class="key">X</span> in zoom flickers between members (same frame = blink in place,
      different shot = everything jumps) &middot; click a card to select.
      Decisions are recorded in the manifest only; <code>dedup apply</code> moves files later.
      {counts['no_clip']:,} video/no-CLIP review clusters not shown (v2).
    </footer>
    <div id="lightbox"><img></div>
    <div id="toast"></div>
    <script>{config}\n{_JS}</script>
    """
    return _page(f"dedup review — cluster {cluster_id}", body)


def render_done(counts: dict) -> str:
    body = f"""
    <div class="done">
      <p>&#127881; No image review clusters left.</p>
      <p>{counts['no_clip']:,} video/no-CLIP review clusters remain for a future pass (v2).</p>
      <p><a href="/">reload</a></p>
    </div>"""
    return _page("dedup review — done", body)


def render_categories(
    grid: dict[tuple[str, int | None], int], counts: dict
) -> str:
    """Cross-table of the pending queue: guard reason × pixel band, every
    non-zero cell a link into batch mode filtered to that category."""
    slugs = sorted({slug for slug, _ in grid}, key=lambda s: -sum(
        n for (sl, _), n in grid.items() if sl == s))
    def link(n, slug=None, band=None):
        if not n:
            return "<td>&middot;</td>"
        params = []
        if slug:
            params.append(f"reason={slug}")
        if band is not None:
            _, lo, hi = PX_BANDS[band]
            if lo is not None:
                params.append(f"min_px={lo}")
            if hi is not None:
                params.append(f"max_px={hi}")
        return f'<td><a href="/batch?{"&amp;".join(params)}">{n}</a></td>'

    header = "".join(f"<th>{html.escape(label)}<br><small>{lo if lo is not None else ''}"
                     f"&ndash;{hi if hi is not None else ''}</small></th>"
                     for label, lo, hi in PX_BANDS) + "<th>unscored</th><th>total</th>"
    rows = []
    for slug in slugs:
        cells = "".join(
            link(grid.get((slug, band), 0), slug, band)
            for band in range(len(PX_BANDS))
        ) + link(grid.get((slug, None), 0), slug=slug)
        total = sum(n for (sl, _), n in grid.items() if sl == slug)
        rows.append(f"<tr><th>{html.escape(slug)}</th>{cells}"
                    f"{link(total, slug=slug)}</tr>")
    col_totals = "".join(
        link(sum(n for (_, b), n in grid.items() if b == band), band=band)
        for band in range(len(PX_BANDS))
    ) + link(sum(n for (_, b), n in grid.items() if b is None))
    grand = sum(grid.values())
    rows.append(f"<tr><th>total</th>{col_totals}{link(grand)}</tr>")

    body = f"""
    <header>
      <h1>review queue by category</h1>
      <span class="progress">{counts['remaining']:,} image clusters pending
        &middot; {counts['no_clip']:,} video/no-CLIP (v2)</span>
      <span class="modeswitch"><a href="/">single</a> &middot; <a href="/batch">batch</a></span>
    </header>
    <table class="cat">
      <tr><th>reason \\ pixel</th>{header}</tr>
      {''.join(rows)}
    </table>
    <footer>Rows: which Stage D guard routed the cluster to review.
      Columns: pixel-identity band from <code>dedup rescore</code>
      (&ge;0.90 leans same-frame duplicate, &le;0.75 leans distinct shots).
      Click any count to review exactly that slice in batch mode.</footer>
    """
    return _page("dedup review — categories", body)


_BATCH_JS = """
// A submitted page must come back at the top — the next batch starts there.
history.scrollRestoration = 'manual';
window.scrollTo(0, 0);

document.addEventListener('click', ev => {
  const row = ev.target.closest('.brow');
  if (row) row.classList.toggle('unchecked');
  updateCount();
});
function updateCount() {
  const n = document.querySelectorAll('.brow:not(.unchecked)').length;
  document.getElementById('mergebtn').textContent =
    'Merge ' + n + ' checked \\u23CE  (unchecked \\u2192 single-mode queue)';
}
let inflight = false;
async function submitBatch() {
  if (inflight) return;
  inflight = true;
  const decisions = [...document.querySelectorAll('.brow:not(.unchecked)')].map(r =>
    ({cluster_id: Number(r.dataset.cid), winner_asset_id: Number(r.dataset.winner)}));
  const skip = [...document.querySelectorAll('.brow.unchecked')].map(r => Number(r.dataset.cid));
  try {
    const res = await fetch('/api/decide-batch', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({decisions, skip})});
    const out = await res.json();
    if (!res.ok) throw new Error(out.error || res.statusText);
    if (out.failed && out.failed.length) alert('some failed: ' + JSON.stringify(out.failed));
    window.location.href = '/batch' + window.location.search;  // keep category filter
  } catch (e) {
    alert('batch failed: ' + e.message);
    inflight = false;
  }
}
async function sweep(action, metric, inputId) {
  if (inflight) return;
  const value = parseFloat(document.getElementById(inputId).value);
  const call = dry => fetch('/api/sweep', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action, metric, value, dry_run: dry})}).then(async r => {
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.statusText);
      return j;
    });
  try {
    const pre = await call(true);
    if (!pre.count) { alert('nothing matches ' + metric + (action === 'keep_all' ? ' \\u2264 ' : ' \\u2265 ') + value); return; }
    const verb = action === 'merge'
      ? 'MERGE (keep recommended winner, rest become losers)'
      : 'mark KEEP-ALL (not duplicates, nothing quarantined)';
    if (!confirm(pre.count + ' clusters with ' + metric +
                 (action === 'keep_all' ? ' \\u2264 ' : ' \\u2265 ') + value + ' \\u2192 ' + verb +
                 '?\\nSame write as deciding each one by hand.')) return;
    inflight = true;
    const out = await call(false);
    alert('decided ' + out.decided +
          (out.failed && out.failed.length ? ', failed ' + out.failed.length : ''));
    window.location.href = '/batch';
  } catch (e) { alert('sweep failed: ' + e.message); inflight = false; }
}
document.addEventListener('keydown', ev => {
  if (ev.target.tagName === 'INPUT') return;
  if (ev.key === 'Enter') { submitBatch(); ev.preventDefault(); }
});
updateCount();
"""


def render_batch(rows: list[dict], counts: dict, filter_note: str = "") -> str:
    """rows: [{cluster_id, clip_cos_sim, members, winner_id}] — compact
    one-line-per-cluster confirm view for the near-certain tail of the
    queue. Everything renders pre-checked; a click toggles a row off
    (unchecked rows go to the session skip list — resurface them in single
    mode after a server restart)."""
    body_rows = []
    for row in rows:
        thumbs = "".join(
            f'<img src="/thumb/{m.id}" loading="lazy" title="{html.escape(m.source)} '
            f'{m.width or "?"}x{m.height or "?"} {human_bytes(m.bytes)}"'
            f'{" class=keepmark" if m.id == row["winner_id"] else ""}>'
            for m in row["members"]
        )
        sim = row["clip_cos_sim"]
        body_rows.append(f"""
        <div class="brow" data-cid="{row['cluster_id']}" data-winner="{row['winner_id']}">
          <div class="check">&#10003;</div>
          {thumbs}
          <div class="bmeta">cluster {row['cluster_id']}<br>
            cos {f"{sim:.6f}" if sim is not None else "—"}<br>
            {len(row['members'])} members<br>
            keeper: {html.escape(next(m.source for m in row['members'] if m.id == row['winner_id']))}<br>
            <span class="reason">{html.escape(review_reason(row['members']))}</span><br>
            {pixel_chip(*row.get('signal', (None, None)))}
          </div>
        </div>""")

    body = f"""
    <header>
      <h1>batch mode</h1>
      <span class="progress">{counts['remaining']:,} remaining &middot;
        showing {len(rows)} &middot; green outline = keeper &middot;
        click a row to uncheck it</span>
      {f'<span class="filternote">filter: {html.escape(filter_note)} &middot; <a href="/batch">clear</a></span>' if filter_note else ''}
      <span class="modeswitch"><a href="/categories">categories</a> &middot;
        <a href="/">single mode</a></span>
    </header>
    <div>{''.join(body_rows)}</div>
    <div class="actions">
      <button class="merge" id="mergebtn" onclick="submitBatch()">Merge checked</button>
      <span class="thresh">
        <input id="mincos" type="number" step="0.0001" min="0.9" max="1" value="0.9990">
        <button class="danger" onclick="sweep('merge','cos','mincos')">Merge ALL &ge; cos</button>
        <input id="minpx" type="number" step="0.001" min="0.9" max="1" value="0.980">
        <button class="danger" onclick="sweep('merge','pixel','minpx')">Merge ALL &ge; pixel</button>
        <input id="maxpx" type="number" step="0.001" min="0" max="0.9" value="0.750">
        <button class="danger" onclick="sweep('keep_all','pixel','maxpx')">Keep-all ALL &le; pixel</button>
      </span>
    </div>
    <footer>Merging keeps the outlined member and marks the rest as losers
      (same manifest write as single mode, originals-guard enforced server-side).
      Unchecked rows are skipped for this session and stay in the queue.
      "Merge ALL &ge; cos" sweeps every remaining cluster at or above the
      threshold with its recommended winner — count and confirm shown first.</footer>
    <script>{_BATCH_JS}</script>
    """
    return _page("dedup review — batch", body)


# --------------------------------------------------------------------- app


def create_app(manifest_path: Path, thumb_root: Path):
    """Flask app factory. One sqlite connection per request (Flask's dev
    server is threaded; sqlite connections don't hop threads), each write
    committed immediately by commit_cluster_decision — the same short-lock
    discipline apply_decisions uses against the shared WAL manifest."""
    from flask import Flask, abort, jsonify, request, send_file

    app = Flask("immy-dedup-review")
    # In-process only: clusters the user skipped this session. No manifest
    # write — a skipped cluster stays decision='review' and reappears after
    # a restart, which is exactly the "come back to it later" semantic.
    skipped: set[int] = set()

    def db() -> sqlite3.Connection:
        return manifest.open_manifest(manifest_path)

    @app.get("/")
    def index():
        conn = db()
        try:
            pending = [cid for cid in _queue_ids(conn) if cid not in skipped]
            if not pending:
                return render_done(_counts(conn))
            # Preload the next couple of clusters' card thumbnails so the
            # page after this decision paints instantly.
            upcoming = pending[1:3]
            prefetch: tuple[int, ...] = ()
            if upcoming:
                marks = ",".join("?" * len(upcoming))
                prefetch = tuple(
                    r[0] for r in conn.execute(
                        f"SELECT asset_id FROM membership WHERE cluster_id IN ({marks})",
                        upcoming,
                    )
                )
            return _render(conn, pending[0], prefetch)
        finally:
            conn.close()

    @app.get("/cluster/<int:cluster_id>")
    def cluster_page(cluster_id: int):
        conn = db()
        try:
            return _render(conn, cluster_id)
        finally:
            conn.close()

    def _render(conn: sqlite3.Connection, cluster_id: int, prefetch: tuple[int, ...] = ()):
        row = conn.execute(
            "SELECT decision, clip_cos_sim FROM cluster WHERE id=?", (cluster_id,)
        ).fetchone()
        if row is None:
            abort(404)
        members = load_cluster_members(conn, cluster_id)
        if not members:
            abort(404)
        return render_cluster(
            cluster_id, row[1], members, _counts(conn), prefetch,
            signal=signals.get_signal(conn, cluster_id),
        )

    @app.post("/api/decide/<int:cluster_id>")
    def decide_cluster(cluster_id: int):
        payload = request.get_json(force=True, silent=True) or {}
        action = payload.get("action")
        if action not in ("merge", "keep_all"):
            return jsonify(error=f"unknown action {action!r}"), 400
        conn = db()
        try:
            row = conn.execute(
                "SELECT decision FROM cluster WHERE id=?", (cluster_id,)
            ).fetchone()
            if row is None:
                return jsonify(error=f"no cluster {cluster_id}"), 404
            if row[0] != "review":
                # Stale tab / double-submit — never silently overwrite a
                # decision that is no longer 'review'.
                return jsonify(
                    error=f"cluster {cluster_id} is already '{row[0]}'"
                ), 409
            members = load_cluster_members(conn, cluster_id)

            if action == "keep_all":
                winner = default_winner(members)
                commit_cluster_decision(
                    conn, cluster_id, members, "kept_all", winner.id,
                    _confidence(members, winner),
                )
                return jsonify(ok=True, decision="kept_all")

            try:
                winner_id = int(payload["winner_asset_id"])
            except (KeyError, TypeError, ValueError):
                return jsonify(error="merge needs winner_asset_id"), 400
            # Server-side originals guard lives in validate_merge — the UI
            # locks this too, but the manifest write is what must never go
            # wrong.
            error, merge_members = validate_merge(conn, cluster_id, winner_id)
            if error:
                return jsonify(error=error[1]), error[0]
            winner = next(m for m in merge_members if m.id == winner_id)
            commit_cluster_decision(
                conn, cluster_id, merge_members, "auto", winner_id,
                _confidence(merge_members, winner),
            )
            return jsonify(ok=True, decision="auto", winner_asset_id=winner_id)
        finally:
            conn.close()

    @app.post("/api/decide-batch")
    def decide_batch():
        payload = request.get_json(force=True, silent=True) or {}
        decisions = payload.get("decisions") or []
        conn = db()
        merged, failed = 0, []
        try:
            for entry in decisions:
                try:
                    cluster_id = int(entry["cluster_id"])
                    winner_id = int(entry["winner_asset_id"])
                except (KeyError, TypeError, ValueError):
                    failed.append({"entry": entry, "error": "malformed"})
                    continue
                error, members = validate_merge(conn, cluster_id, winner_id)
                if error:
                    failed.append({"cluster_id": cluster_id, "error": error[1]})
                    continue
                winner = next(m for m in members if m.id == winner_id)
                # One commit per cluster (inside commit_cluster_decision) —
                # a mid-batch crash loses nothing already reported merged.
                commit_cluster_decision(
                    conn, cluster_id, members, "auto", winner_id,
                    _confidence(members, winner),
                )
                merged += 1
            for cid in payload.get("skip") or []:
                skipped.add(int(cid))
            return jsonify(ok=True, merged=merged, failed=failed)
        finally:
            conn.close()

    @app.post("/api/sweep")
    def sweep():
        """Sweep every pending cluster past a threshold with one decision,
        each cluster getting its recommended (originals-locked) winner. The
        human authorizes a band wholesale after eyeballing its head —
        equivalent to pressing Enter (or K) on each one, not a new kind of
        write. Session-skipped clusters are excluded: a skip means 'unsure',
        and a sweep must never override an explicit human hesitation.

        Shapes:
          merge    + cos   >= v  (v > 0.9)  — the CLIP-identical band
          merge    + pixel >= v  (v > 0.9)  — same-frame re-exports (rescore)
          keep_all + pixel <= v  (v < 0.9)  — distinct shots of one scene
        keep_all on cos is refused: scene similarity can't prove distinctness.
        """
        payload = request.get_json(force=True, silent=True) or {}
        action = payload.get("action")
        metric = payload.get("metric")
        try:
            value = float(payload["value"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="value required"), 400
        if action not in ("merge", "keep_all") or metric not in ("cos", "pixel"):
            return jsonify(error="action must be merge|keep_all, metric cos|pixel"), 400
        if action == "merge" and value <= 0.9:
            return jsonify(error="refusing a merge sweep below 0.9"), 400
        if action == "keep_all" and (metric != "pixel" or value >= 0.9):
            return jsonify(
                error="keep_all sweeps need metric=pixel with a bar below 0.9"
            ), 400

        if metric == "cos":
            sql = ("SELECT id FROM cluster WHERE decision='review'"
                   " AND clip_cos_sim >= ? ORDER BY clip_cos_sim DESC, id ASC")
        elif action == "merge":
            sql = ("SELECT c.id FROM cluster c JOIN review_signal s ON s.cluster_id=c.id"
                   " WHERE c.decision='review' AND s.pixel_ncc >= ?"
                   " ORDER BY s.pixel_ncc DESC, c.id ASC")
        else:
            sql = ("SELECT c.id FROM cluster c JOIN review_signal s ON s.cluster_id=c.id"
                   " WHERE c.decision='review' AND s.pixel_ncc <= ?"
                   " ORDER BY s.pixel_ncc ASC, c.id ASC")

        conn = db()
        try:
            try:
                ids = [cid for (cid,) in conn.execute(sql, (value,)) if cid not in skipped]
            except sqlite3.OperationalError:
                return jsonify(error="no pixel scores yet — run `immy dedup rescore` first"), 400
            if payload.get("dry_run"):
                return jsonify(count=len(ids))
            done, failed = 0, []
            for cid in ids:
                members = load_cluster_members(conn, cid)
                if not members:
                    continue
                winner = default_winner(members)
                if action == "merge":
                    error, checked = validate_merge(conn, cid, winner.id)
                    if error:
                        failed.append({"cluster_id": cid, "error": error[1]})
                        continue
                    commit_cluster_decision(
                        conn, cid, checked, "auto", winner.id,
                        _confidence(checked, winner),
                    )
                else:
                    still = conn.execute(
                        "SELECT decision FROM cluster WHERE id=?", (cid,)
                    ).fetchone()
                    if not still or still[0] != "review":
                        continue
                    commit_cluster_decision(
                        conn, cid, members, "kept_all", winner.id,
                        _confidence(members, winner),
                    )
                done += 1
            return jsonify(ok=True, decided=done, failed=failed[:10])
        finally:
            conn.close()

    @app.get("/categories")
    def categories_page():
        conn = db()
        try:
            grid: dict[tuple[str, int | None], int] = {}
            for _cid, members, px in _load_all_pending(conn, skipped):
                key = (reason_slug(members), _band_of(px))
                grid[key] = grid.get(key, 0) + 1
            return render_categories(grid, _counts(conn))
        finally:
            conn.close()

    @app.get("/batch")
    def batch_page():
        args = request.args
        min_px = args.get("min_px", type=float)
        max_px = args.get("max_px", type=float)
        reason = args.get("reason")
        notes = []
        if reason:
            notes.append(reason)
        if min_px is not None:
            notes.append(f"px ≥ {min_px}")
        if max_px is not None:
            notes.append(f"px < {max_px}")
        conn = db()
        try:
            selected = []
            for cid, members, px in _load_all_pending(conn, skipped):
                if min_px is not None and (px is None or px < min_px):
                    continue
                if max_px is not None and (px is None or px >= max_px):
                    continue
                if reason and reason_slug(members) != reason:
                    continue
                selected.append((cid, members, px))
                if len(selected) >= BATCH_SIZE:
                    break
            if not selected:
                return render_done(_counts(conn))
            rows = []
            for cid, members, _px in selected:
                sim = conn.execute(
                    "SELECT clip_cos_sim FROM cluster WHERE id=?", (cid,)
                ).fetchone()[0]
                rows.append({
                    "cluster_id": cid,
                    "clip_cos_sim": sim,
                    "members": members,
                    "winner_id": default_winner(members).id,
                    "signal": signals.get_signal(conn, cid),
                })
            return render_batch(rows, _counts(conn), ", ".join(notes))
        finally:
            conn.close()

    @app.post("/api/skip/<int:cluster_id>")
    def skip_cluster(cluster_id: int):
        skipped.add(cluster_id)
        return jsonify(ok=True, skipped=len(skipped))

    @app.get("/thumb/<int:asset_id>")
    def thumb(asset_id: int):
        size = LIGHTBOX_SIZE if request.args.get("size") == "large" else THUMB_SIZE
        dest = thumb_root / str(size) / f"{asset_id}.jpg"
        if not dest.exists():
            conn = db()
            try:
                row = conn.execute(
                    "SELECT path FROM asset WHERE id=?", (asset_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                abort(404)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not make_thumb(row[0], dest, size):
                abort(404)
        return send_file(dest, mimetype="image/jpeg", max_age=86400)

    return app


def _warm_thumbs(manifest_path: Path, thumb_root: Path) -> None:
    """Walk the review queue in serving order and pre-generate every card
    thumbnail that's missing. Runs as a daemon thread beside the server so
    the front of the queue is warm within seconds and generation stays ahead
    of human review pace — first paint of each cluster then costs a cache
    read, not a decode. Lightbox (1600px) thumbs stay lazy: zoom is the rare
    action, warming them would double the work for little gain."""
    conn = manifest.open_manifest(manifest_path)
    try:
        for cid in _queue_ids(conn):
            for m in load_cluster_members(conn, cid):
                dest = thumb_root / str(THUMB_SIZE) / f"{m.id}.jpg"
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    make_thumb(m.path, dest, THUMB_SIZE)
    except Exception:
        pass  # warming is best-effort; requests fall back to lazy generation
    finally:
        conn.close()


def serve(manifest_path: Path, thumb_root: Path, host: str, port: int) -> None:
    app = create_app(manifest_path, thumb_root)
    threading.Thread(
        target=_warm_thumbs, args=(manifest_path, thumb_root),
        daemon=True, name="thumb-warmer",
    ).start()
    app.run(host=host, port=port, threaded=True)
