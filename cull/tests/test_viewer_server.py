import json
import threading
import urllib.error
import urllib.request

import pytest

from cull.core.jsonio import write_json_atomic
from cull.scan import media_url
from cull.viewer import server as vs

ODD = "Tesla Service Flight 'Monroky'/25Dec+Picknick #1 100%/DJI_0001.MOV"


@pytest.fixture
def site(tmp_path):
    src = tmp_path / "src"
    (src / ODD).parent.mkdir(parents=True)
    (src / ODD).write_bytes(b"0123456789")
    (tmp_path / "secret.txt").write_text("no")
    work = tmp_path / "work"
    (work / "_cull" / "thumbs").mkdir(parents=True)
    (work / "_cull" / "thumbs" / "t.jpg").write_bytes(b"jpg")
    (work / "_cull" / "decisions.json").write_text("{}")
    item = {"id": f"audiq7/{ODD}", "src": "audiq7", "files": [ODD], "fp": "f1", "kind": "video"}
    write_json_atomic(work / "_cull" / "items.json",
                      {"schema": 1, "sources": {"audiq7": str(src)}, "items": [item], "orphans": []})
    trips = tmp_path / "trips"
    (trips / "2023-08-croatia").mkdir(parents=True)
    srv = vs.make_server(work, trips, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "work": work, "item": item}
    srv.shutdown()
    srv.server_close()


def call(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_index_items_trips(site):
    assert call(site["base"], "/")[0] == 200
    code, _, body = call(site["base"], "/api/items")
    assert code == 200 and json.loads(body)["items"][0]["id"] == site["item"]["id"]
    assert json.loads(call(site["base"], "/api/trips")[2]) == ["2023-08-croatia"]


def test_odd_names_round_trip_through_media_urls(site):
    url = media_url("audiq7", ODD)
    code, hdr, body = call(site["base"], url, headers={"Range": "bytes=2-4"})
    assert code == 206 and body == b"234" and hdr["Content-Type"] == "video/quicktime"


def test_media_and_work_escapes(site):
    assert call(site["base"], "/media/audiq7/../secret.txt")[0] == 404
    assert call(site["base"], "/media/nosuch/x.mp4")[0] == 404
    assert call(site["base"], "/work/thumbs/t.jpg")[0] == 200
    assert call(site["base"], "/work/decisions.json")[0] == 404
    assert call(site["base"], "/work/items.json")[0] == 404


def test_post_decisions(site):
    iid = site["item"]["id"]
    code, _, body = call(site["base"], "/api/decisions", "POST",
                         {"ids": [iid], "patch": {"keep": "keep", "trip": "2023-08-croatia"}})
    assert code == 200 and json.loads(body)["decisions"][iid]["trip"] == "2023-08-croatia"
    assert json.loads(call(site["base"], "/api/decisions")[2])[iid]["keep"] == "keep"
    code, _, body = call(site["base"], "/api/decisions", "POST", {"ids": [iid], "patch": {"trip": "Bad Name"}})
    assert code == 400 and "trip" in json.loads(body)["error"]
    code, _, _ = call(site["base"], "/api/decisions", "POST", {"ids": [iid], "patch": {"keep": "drop"}},
                      headers={"Origin": "http://evil.example"})
    assert code == 403


def test_viewer_page_wires_the_api(site):
    code, hdr, body = call(site["base"], "/")
    assert code == 200 and hdr["Content-Type"].startswith("text/html")
    for needle in (b"/api/items", b"/api/decisions", b"/api/trips", b"'Content-Type':'application/json'",
                   b"add_tags", b"possibly in Immich"):
        assert needle in body, needle
