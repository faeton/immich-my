"""360 panorama viewer — the drag-around player Immich doesn't have.

Immich renders 360 photos but plays 360 video flat (feature request
declined upstream), so this sidecar serves the library's Insta360
recordings properly: a per-trip grid, and a WebGL equirectangular player
(fullscreen quad + reprojection shader, zero JS dependencies) that streams
straight off the originals with Range requests.

What it streams, per recording, best-first:
  LRV_*_11_*.insv   the camera's stitched equirect preview — small, always
                    present, and the reason those files are worth keeping
  VID_*.mp4 export  full-res stitch when one exists (offered as a source
                    toggle — an export may be reframed flat, so the player
                    lets the human flip projection too)
The raw VID_ _00_/_10_ fisheye masters are never streamed: unstitched
hemispheres are unwatchable, and those files are cold-tier archive for
Insta360 Studio, not for browsers.

Read-only over the manifest + filesystem: no Immich API, no writes, no
transcodes. Posters are lazily extracted (one 480px JPEG per recording)
into the frames cache.
"""

from __future__ import annotations

import html
import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .dedup import manifest
from .stacks import _NAME
from .triage.engine import map_path, trip_of

POSTER_WIDTH = 480


# ------------------------------------------------------------------ model


@dataclass
class Recording:
    key: str                       # "<ts>_<serial>" — URL-safe group id
    trip: str
    ts: str                        # "YYYYMMDD_HHMMSS"
    masters: list[int] = field(default_factory=list)
    master_bytes: int = 0
    lrv_id: int | None = None
    export_id: int | None = None
    duration_s: float | None = None

    @property
    def stream_id(self) -> int | None:
        return self.lrv_id if self.lrv_id is not None else self.export_id

    def when(self) -> str:
        try:
            return datetime.strptime(self.ts, "%Y%m%d_%H%M%S").strftime(
                "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return self.ts


def load_recordings(conn: sqlite3.Connection, root: str) -> list[Recording]:
    """Group originals into 360 recordings by the camera's timestamp+serial
    (same key `stacks` uses). Only groups holding at least one .insv lens
    master count — mp4-only groups are flat footage Immich already handles."""
    durations = dict(conn.execute(
        "SELECT asset_id, duration_s FROM video_signal"
    ))
    recs: dict[str, Recording] = {}
    for asset_id, path in conn.execute(
        "SELECT id, path FROM asset WHERE source='originals'"
        " AND (path LIKE '%.insv' OR path LIKE '%.mp4' OR path LIKE '%.lrv'"
        "  OR path LIKE '%.INSV' OR path LIKE '%.MP4' OR path LIKE '%.LRV')"
    ):
        trip = trip_of(path, root)
        if trip is None:
            continue
        m = _NAME.match(path.rsplit("/", 1)[-1])
        if not m:
            continue
        key = f"{m['ts']}_{m['serial']}"
        rec = recs.setdefault(key, Recording(key=key, trip=trip, ts=m["ts"]))
        role, lens, ext = m["role"].lower(), m["lens"], m["ext"].lower()
        if role == "lrv" and ext in ("insv", "lrv"):
            # stitched in-camera preview — X3 era: LRV_*_11_*.insv,
            # X4/X5 era: LRV_*_01_*.lrv. Both equirect h264.
            rec.lrv_id = asset_id
        elif role == "vid" and ext == "mp4":
            rec.export_id = asset_id
        elif role == "vid" and ext == "insv":
            rec.masters.append(asset_id)
            rec.master_bytes += conn.execute(
                "SELECT bytes FROM asset WHERE id=?", (asset_id,)
            ).fetchone()[0] or 0
            if rec.duration_s is None:
                rec.duration_s = durations.get(asset_id)
    out = [r for r in recs.values() if r.masters]
    out.sort(key=lambda r: (r.trip, r.ts))
    return out


def human_bytes(n) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fmt_duration(seconds) -> str:
    if not seconds or seconds <= 0:
        return "?"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def extract_poster(src: Path, dst: Path, seek_s: float) -> bool:
    """One 480px JPEG from the stitched preview — equirect looks fine as a
    wide little banner. Best-effort; a failure renders as a gray tile."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.jpg")
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(seek_s, 0.0):.1f}", "-i", str(src),
         "-vf", f"scale={POSTER_WIDTH}:-2", "-frames:v", "1", "-q:v", "4",
         "-y", str(tmp)],
        capture_output=True,
    )
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(dst)
        return True
    tmp.unlink(missing_ok=True)
    return False


# ------------------------------------------------------------------- pages


_CSS = """
:root{--bg:#0d0d0f;--panel:#17171a;--edge:#2c2c30;--fg:#eee;--dim:#999}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px 20px}
a{color:#7ab7ff;text-decoration:none}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:1.05rem;margin:0}
.progress{color:var(--dim);font-size:.85rem}
.trips{display:flex;flex-direction:column;gap:8px;max-width:560px}
.triprow{display:flex;gap:12px;align-items:baseline;background:var(--panel);
         border:1px solid var(--edge);border-radius:10px;padding:12px 16px}
.triprow .n{color:var(--dim);font-size:.85rem;margin-left:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:10px;
      overflow:hidden;cursor:pointer}
.card:hover{border-color:#4a6a9a}
.card img{display:block;width:100%;aspect-ratio:2/1;object-fit:cover;background:#000}
.card .noposter{width:100%;aspect-ratio:2/1;display:flex;align-items:center;
      justify-content:center;color:#555;background:#151518}
.card .meta{padding:8px 12px;font-size:.78rem;color:#bbb;display:flex;gap:10px}
.badge{border:1px solid #3a5a3a;color:#9fdca4;border-radius:4px;padding:0 6px;font-size:.7rem}
.badge.lrv{border-color:#3a4a6a;color:#9fc0dc}
#player{position:fixed;inset:0;background:#000;display:none;z-index:10}
#player canvas{position:absolute;inset:0;width:100%;height:100%;cursor:grab}
#player canvas.dragging{cursor:grabbing}
#player video{display:none}
#player video.flat{display:block;position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
#hud{position:absolute;left:0;right:0;bottom:0;display:flex;gap:10px;align-items:center;
     padding:14px 18px;background:linear-gradient(transparent,#000c);font-size:.85rem;z-index:2}
#hud button{font-size:.8rem;padding:6px 12px;border-radius:8px;border:1px solid #555;
     background:#222a;color:#eee;cursor:pointer}
#hud .title{color:#ccc;margin-right:auto}
#seek{flex:1;max-width:40vw}
.key{display:inline-block;min-width:1.2em;text-align:center;background:#2c2c2c;border:1px solid #444;
     border-radius:4px;padding:0 4px;margin:0 2px;font-family:ui-monospace,monospace;font-size:.75rem}
footer{margin-top:18px;color:#666;font-size:.75rem;line-height:1.7}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_index(recs: list[Recording]) -> str:
    trips: dict[str, dict] = {}
    for r in recs:
        t = trips.setdefault(r.trip, {"n": 0, "bytes": 0})
        t["n"] += 1
        t["bytes"] += r.master_bytes
    rows = "".join(
        f"<div class='triprow'><a href='/trip/{html.escape(trip)}'>{html.escape(trip)}</a>"
        f"<span class='n'>{t['n']} recordings &middot; {human_bytes(t['bytes'])}</span></div>"
        for trip, t in sorted(trips.items(), reverse=True)
    )
    body = f"""
    <header><h1>&#127760; 360 recordings</h1>
      <span class="progress">{len(recs)} recordings &middot;
        {sum(t['n'] for t in trips.values())} across {len(trips)} trips</span>
    </header>
    <div class="trips">{rows}</div>
    <footer>Streams the stitched in-camera previews (and full-res exports where
      they exist) with a proper drag-around 360 projection — the raw fisheye
      masters stay untouched for Insta360 Studio.</footer>
    """
    return _page("360 viewer", body)


_PLAYER_JS = r"""
const RECS = RECS_JSON;
let cur = -1, gl = null, tex = null, prog = null;
let yaw = 0, pitch = 0, fov = 1.35, projecting = true;

const video = document.querySelector('#player video');
const canvas = document.querySelector('#player canvas');

function initGL() {
  gl = canvas.getContext('webgl');
  const vs = `attribute vec2 p; varying vec2 ndc;
    void main(){ ndc = p; gl_Position = vec4(p, 0.0, 1.0); }`;
  const fs = `precision mediump float; varying vec2 ndc;
    uniform float yaw, pitch, fov, aspect; uniform sampler2D tex;
    void main(){
      float f = 1.0 / tan(fov * 0.5);
      vec3 d = normalize(vec3(ndc.x * aspect, ndc.y, -f));
      float cp = cos(pitch), sp = sin(pitch);
      d = vec3(d.x, d.y*cp - d.z*sp, d.y*sp + d.z*cp);
      float cy = cos(yaw), sy = sin(yaw);
      d = vec3(d.x*cy + d.z*sy, d.y, -d.x*sy + d.z*cy);
      float u = 0.5 + atan(d.x, -d.z) / 6.28318530718;
      float v = 0.5 - asin(clamp(d.y, -1.0, 1.0)) / 3.14159265359;
      gl_FragColor = texture2D(tex, vec2(u, v));
    }`;
  function shader(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
      throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  prog = gl.createProgram();
  gl.attachShader(prog, shader(gl.VERTEX_SHADER, vs));
  gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog); gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER,
    new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
}

function frame() {
  if (document.getElementById('player').style.display !== 'block') return;
  if (projecting && gl) {
    if (canvas.width !== canvas.clientWidth || canvas.height !== canvas.clientHeight) {
      canvas.width = canvas.clientWidth; canvas.height = canvas.clientHeight;
      gl.viewport(0, 0, canvas.width, canvas.height);
    }
    if (video.readyState >= 2)
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, video);
    gl.uniform1f(gl.getUniformLocation(prog, 'yaw'), yaw);
    gl.uniform1f(gl.getUniformLocation(prog, 'pitch'), pitch);
    gl.uniform1f(gl.getUniformLocation(prog, 'fov'), fov);
    gl.uniform1f(gl.getUniformLocation(prog, 'aspect'), canvas.width / canvas.height);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
  const seek = document.getElementById('seek');
  if (video.duration && !seek.dragging)
    seek.value = 1000 * video.currentTime / video.duration;
  requestAnimationFrame(frame);
}

function openRec(i) {
  cur = i;
  const r = RECS[i];
  if (!gl) initGL();
  document.getElementById('player').style.display = 'block';
  document.querySelector('#hud .title').textContent =
    r.when + ' · ' + r.dur + ' · masters ' + r.size;
  document.getElementById('srcbtn').style.display = r.export ? '' : 'none';
  yaw = 0; pitch = 0; fov = 1.35;
  setSource(r.stream);
  setProjection(true);
}
function setSource(assetId) {
  video.src = '/stream/' + assetId;
  video.play().catch(() => {});
}
function setProjection(on) {
  projecting = on;
  canvas.style.display = on ? 'block' : 'none';
  video.classList.toggle('flat', !on);
  document.getElementById('projbtn').textContent = on ? 'flat view' : '360 view';
}
function closePlayer() {
  video.pause(); video.removeAttribute('src'); video.load();
  document.getElementById('player').style.display = 'none';
}

document.addEventListener('click', ev => {
  const card = ev.target.closest('.card');
  if (card) { openRec(Number(card.dataset.i)); frame(); }
});

// Deep link from the triage UI: /trip/<trip>?open=<key> lands straight in
// the player on that recording.
const deepLink = new URLSearchParams(location.search).get('open');
if (deepLink) {
  const i = RECS.findIndex(r => r.key === deepLink);
  if (i >= 0) { openRec(i); frame(); }
}
document.getElementById('closebtn').addEventListener('click', closePlayer);
document.getElementById('playbtn').addEventListener('click',
  () => video.paused ? video.play() : video.pause());
document.getElementById('projbtn').addEventListener('click',
  () => setProjection(!projecting));
document.getElementById('srcbtn').addEventListener('click', () => {
  const r = RECS[cur];
  const usingLrv = video.src.endsWith('/stream/' + r.stream) && r.stream === r.lrv;
  setSource(usingLrv && r.export ? r.export : r.stream);
});
document.getElementById('fsbtn').addEventListener('click', () =>
  document.fullscreenElement ? document.exitFullscreen()
    : document.getElementById('player').requestFullscreen());
const seek = document.getElementById('seek');
seek.addEventListener('input', () => {
  if (video.duration) video.currentTime = video.duration * seek.value / 1000;
});
seek.addEventListener('pointerdown', () => seek.dragging = true);
seek.addEventListener('pointerup', () => seek.dragging = false);

let drag = null;
canvas.addEventListener('pointerdown', ev => {
  drag = {x: ev.clientX, y: ev.clientY, yaw, pitch};
  canvas.classList.add('dragging');
  canvas.setPointerCapture(ev.pointerId);
});
canvas.addEventListener('pointermove', ev => {
  if (!drag) return;
  const scale = fov / canvas.clientHeight;
  yaw = drag.yaw - (ev.clientX - drag.x) * scale;
  pitch = Math.max(-1.55, Math.min(1.55, drag.pitch + (ev.clientY - drag.y) * scale));
});
canvas.addEventListener('pointerup', () => {
  drag = null; canvas.classList.remove('dragging');
});
canvas.addEventListener('wheel', ev => {
  fov = Math.max(0.45, Math.min(2.4, fov + ev.deltaY * 0.002));
  ev.preventDefault();
}, {passive: false});
canvas.addEventListener('dblclick', () => document.getElementById('fsbtn').click());

document.addEventListener('keydown', ev => {
  if (document.getElementById('player').style.display !== 'block') return;
  if (ev.key === 'Escape') closePlayer();
  else if (ev.key === ' ') { document.getElementById('playbtn').click(); ev.preventDefault(); }
  else if (ev.key === 'ArrowRight' && !ev.shiftKey) video.currentTime += 10;
  else if (ev.key === 'ArrowLeft' && !ev.shiftKey) video.currentTime -= 10;
  else if (ev.key === 'ArrowDown' && cur + 1 < RECS.length) openRec(cur + 1);
  else if (ev.key === 'ArrowUp' && cur > 0) openRec(cur - 1);
  else if (ev.key.toLowerCase() === 'f') document.getElementById('fsbtn').click();
});
"""


def render_trip(trip: str, recs: list[Recording]) -> str:
    cards, js = [], []
    for i, r in enumerate(recs):
        js.append({
            "key": r.key, "when": r.when(), "dur": fmt_duration(r.duration_s),
            "size": human_bytes(r.master_bytes),
            "stream": r.stream_id, "lrv": r.lrv_id, "export": r.export_id,
        })
        badge = (
            "<span class='badge'>full-res export</span>" if r.export_id
            else "<span class='badge lrv'>camera preview</span>"
        )
        cards.append(f"""
        <div class="card" data-i="{i}">
          <img src="/poster/{r.key}" loading="lazy"
               onerror="this.outerHTML='<div class=noposter>no preview</div>'">
          <div class="meta"><span>{r.when()}</span>
            <span>{fmt_duration(r.duration_s)}</span>
            <span>{human_bytes(r.master_bytes)}</span>{badge}</div>
        </div>""")
    body = f"""
    <header><h1><a href="/">&larr;</a> {html.escape(trip)}</h1>
      <span class="progress">{len(recs)} recordings &middot; drag to look
        around &middot; wheel zooms &middot; <span class="key">&darr;</span>
        next &middot; <span class="key">F</span> fullscreen &middot;
        <span class="key">Esc</span> closes</span>
    </header>
    <div class="grid">{''.join(cards)}</div>
    <div id="player">
      <video playsinline></video><canvas></canvas>
      <div id="hud">
        <span class="title"></span>
        <input id="seek" type="range" min="0" max="1000" value="0">
        <button id="playbtn">&#9199;</button>
        <button id="projbtn">flat view</button>
        <button id="srcbtn">full-res source</button>
        <button id="fsbtn">fullscreen</button>
        <button id="closebtn">&times;</button>
      </div>
    </div>
    <script>const RECS_JSON = {json.dumps(js)};\n{_PLAYER_JS}</script>
    """
    return _page(f"360 — {trip}", body)


# --------------------------------------------------------------------- app


def create_app(
    manifest_path: Path, poster_root: Path, root: str, fs_root: str | None
):
    from flask import Flask, abort, send_file

    app = Flask("immy-360-viewer")
    fs_root = fs_root or root

    def db() -> sqlite3.Connection:
        return manifest.open_manifest(manifest_path)

    def recordings() -> list[Recording]:
        conn = db()
        try:
            return load_recordings(conn, root)
        finally:
            conn.close()

    @app.get("/")
    def index():
        return render_index(recordings())

    @app.get("/trip/<trip>")
    def trip_page(trip: str):
        recs = [r for r in recordings() if r.trip == trip]
        if not recs:
            abort(404)
        return render_trip(trip, recs)

    @app.get("/poster/<key>")
    def poster(key: str):
        dst = poster_root / f"{key}.jpg"
        if not dst.exists():
            rec = next((r for r in recordings() if r.key == key), None)
            if rec is None or rec.stream_id is None:
                abort(404)
            conn = db()
            try:
                row = conn.execute(
                    "SELECT path FROM asset WHERE id=?", (rec.stream_id,)
                ).fetchone()
            finally:
                conn.close()
            src = map_path(row[0], root, fs_root)
            seek = (rec.duration_s or 4.0) * 0.25
            if not extract_poster(src, dst, seek):
                abort(404)
        return send_file(dst, mimetype="image/jpeg", max_age=86400)

    @app.get("/stream/<int:asset_id>")
    def stream(asset_id: int):
        conn = db()
        try:
            row = conn.execute(
                "SELECT path FROM asset WHERE id=?", (asset_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        src = map_path(row[0], root, fs_root)
        if not src.exists():
            abort(404)
        # .insv IS an mp4 container (plus a proprietary trailer browsers
        # ignore) — served as video/mp4, Range-enabled so seeking works.
        return send_file(src, mimetype="video/mp4", conditional=True)

    return app


def serve(
    manifest_path: Path, poster_root: Path, root: str, fs_root: str | None,
    host: str, port: int,
) -> None:
    app = create_app(manifest_path, poster_root, root, fs_root)
    app.run(host=host, port=port, threaded=True)
