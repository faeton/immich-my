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

from . import manifest
from .engine import (
    AssetLite,
    _confidence,
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


def default_winner(members: list[AssetLite]) -> AssetLite:
    """Pre-selected keeper: same heuristic decide() uses — except when the
    cluster contains a library/originals member, which is pre-locked as the
    winner (promoting a different member over an already-canonical file is a
    swap, not a promote — see _decide_one's originals guard)."""
    originals = [m for m in members if m.source == "originals"]
    pool = originals or members
    return max(pool, key=winner_score)


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
.brow img{height:180px;max-width:260px;object-fit:contain;background:#000;border-radius:4px}
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

function lightbox(assetId) {
  const box = document.getElementById('lightbox');
  box.querySelector('img').src = '/thumb/' + assetId + '?size=large';
  box.style.display = 'flex';
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
  if (box.style.display === 'flex' && (ev.key === 'Escape' || ev.key === 'Enter')) {
    box.style.display = 'none'; ev.preventDefault(); return;
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
) -> str:
    suggested = default_winner(members)
    locked = any(m.source == "originals" for m in members)

    cards = []
    for i, m in enumerate(members):
        key_hint = f"<span class='key'>{i + 1}</span>" if i < 9 else ""
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
            &middot; <span class="score">score {winner_score(m):.0f}</span>
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
    sim = f"{clip_cos_sim:.4f}" if clip_cos_sim is not None else "—"
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
      <span class="modeswitch"><a href="/batch">batch mode &rarr;</a></span>
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
      <span class="key">Z</span>/space or alt-click zoom &middot; click a card to select.
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


_BATCH_JS = """
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
    window.location.reload();
  } catch (e) {
    alert('batch failed: ' + e.message);
    inflight = false;
  }
}
document.addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { submitBatch(); ev.preventDefault(); }
});
updateCount();
"""


def render_batch(rows: list[dict], counts: dict) -> str:
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
            cos {f"{sim:.4f}" if sim is not None else "—"}<br>
            {len(row['members'])} members<br>
            keeper: {html.escape(next(m.source for m in row['members'] if m.id == row['winner_id']))}
          </div>
        </div>""")

    body = f"""
    <header>
      <h1>batch mode</h1>
      <span class="progress">{counts['remaining']:,} remaining &middot;
        showing {len(rows)} &middot; green outline = keeper &middot;
        click a row to uncheck it</span>
      <span class="modeswitch"><a href="/">&larr; single mode</a></span>
    </header>
    <div>{''.join(body_rows)}</div>
    <div class="actions">
      <button class="merge" id="mergebtn" onclick="submitBatch()">Merge checked</button>
    </div>
    <footer>Merging keeps the outlined member and marks the rest as losers
      (same manifest write as single mode, originals-guard enforced server-side).
      Unchecked rows are skipped for this session and stay in the queue.</footer>
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
        return render_cluster(cluster_id, row[1], members, _counts(conn), prefetch)

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

    @app.get("/batch")
    def batch_page():
        conn = db()
        try:
            pending = [cid for cid in _queue_ids(conn) if cid not in skipped]
            if not pending:
                return render_done(_counts(conn))
            rows = []
            for cid in pending[:BATCH_SIZE]:
                sim = conn.execute(
                    "SELECT clip_cos_sim FROM cluster WHERE id=?", (cid,)
                ).fetchone()[0]
                members = load_cluster_members(conn, cid)
                if not members:
                    continue
                rows.append({
                    "cluster_id": cid,
                    "clip_cos_sim": sim,
                    "members": members,
                    "winner_id": default_winner(members).id,
                })
            return render_batch(rows, _counts(conn))
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
