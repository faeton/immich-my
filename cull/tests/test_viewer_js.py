"""Run the viewer's inline <script> in node against a stubbed DOM."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).parents[1] / "src" / "cull" / "viewer" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HARNESS = r"""
const vm = require('vm');
const [script, filter, fail] = [process.argv[2], process.argv[3], process.argv[4] === 'fail'];
const els = {};
const el = id => els[id] ??= {id, value: id === 'f' ? filter : '', innerHTML: '', textContent: '', dataset: {},
  blur() {}, focus() {}, select() {}, click() {}, matches: () => false, closest: () => null};
const items = 'ABCDE'.split('').map((c, n) => ({id: c, src: 'S', kind: 'photo', time: `2024-05-01T10:0${n}:00`,
  size: 1, flags: [], immich: [], files: [c], image: '/x', thumb: '/x', time_src: 'exif', lat: null, place: null}));
const dec = Object.fromEntries(items.map(i => [i.id, {keep: 'unreviewed', trip: null, tags: [], note: '', changed: false}]));
const posted = [];
const json = o => Promise.resolve({ok: true, status: 200, json: async () => o});
const ctx = {document: {getElementById: el, querySelector: () => null, onkeydown: null}, console,
  fetch: async (url, opt) => {
    if (opt && opt.method === 'POST') {
      if (fail) throw new TypeError('offline');
      const b = JSON.parse(opt.body);
      posted.push(b.ids);
      for (const id of b.ids) Object.assign(dec[id], b.patch);
      return json({decisions: Object.fromEntries(b.ids.map(id => [id, dec[id]]))});
    }
    if (url === '/api/items') return json({items, sources: {S: '/s'}, orphans: []});
    if (url === '/api/decisions') return json(dec);
    return json([]);
  }};
vm.createContext(ctx);
vm.runInContext(script, ctx);
(async () => {
  await new Promise(r => setTimeout(r, 20));
  const run = c => vm.runInContext(c, ctx);
  const shown = [];
  run('(function(){const s=show;show=function(id){__shown.push(id);return s(id)}})()'.replace('__shown', 'globalThis.__shown'));
  ctx.__shown = shown;
  const out = {};
  if (process.argv[5] === 'marks') {
    for (let n = 0; n < 3; n++) await run("mark('keep')");
    out.posted = posted;
    out.shown = shown;
    out.cur = run('cur');
  } else {
    const ok = await run("post({keep:'keep'})");
    out.ok = ok;
    out.err = el('err').textContent;
  }
  console.log(JSON.stringify(out));
})();
"""


def run_node(tmp_path, filter_, mode, fail=False):
    script = re.search(r"<script>(.*?)</script>", HTML.read_text(), re.S).group(1)
    h = tmp_path / "h.js"
    h.write_text(HARNESS)
    r = subprocess.run(["node", str(h), script, filter_, "fail" if fail else "ok", mode],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_marking_under_a_hiding_filter_does_not_skip_items(tmp_path):
    out = run_node(tmp_path, "unreviewed", "marks")
    assert out["posted"] == [["A"], ["B"], ["C"]]
    assert out["cur"] == "D"
    # one show per mark (the pick), never a jump to the first item mid-mark
    assert out["shown"] == ["B", "C", "D"]


def test_network_failure_is_reported_not_thrown(tmp_path):
    out = run_node(tmp_path, "all", "post", fail=True)
    assert out == {"ok": False, "err": "network error — not saved"}
