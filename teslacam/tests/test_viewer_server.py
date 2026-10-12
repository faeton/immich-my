"""teslacam.viewer.server: API, decisions merge, Range, path escapes.  Loopback only."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from teslacam.viewer import server as vs

EVENT_ID = "DumpA|SavedClips|2026-09-09_19-16-35"
EVENT = {
    "id": EVENT_ID, "dump": "DumpA", "kind": "saved",
    "dir": "DumpA/SavedClips/2026-09-09_19-16-35",
    "cams": ["front", "back"],
    "segs": [{"t": "2026-09-09_19-15-35", "d": 60.0, "start": 0.0,
              "f": {"front": "a-front.mp4", "back": "a-back.mp4"}},
             {"t": "2026-09-09_19-16-35", "d": 40.0, "start": 60.0,
              "f": {"front": "b-front.mp4", "back": "b-back.mp4"}}],
    "total": 100.0, "trigger_s": 98.0, "ts": "2026-09-09T19:16:35",
    "offset": "+02:00", "offset_source": "gps", "lat": 37.17, "lon": -3.59, "gps_source": "sei",
    "city": "Granada", "reason": "user_interaction_honk", "reason_slug": "honk",
    "sei": True, "bad": 0, "thumb": "thumbs/DumpA__SavedClips__2026-09-09_19-16-35.jpg",
}
OTHER_ID = "DumpB|SentryClips|2026-09-10_01-00-00"
OTHER = {**EVENT, "id": OTHER_ID, "dump": "DumpB", "kind": "sentry", "dir": "DumpB/SentryClips/2026-09-10_01-00-00"}
BLOB = bytes(range(256)) * 4  # 1024 bytes
THUMB = b"\xff\xd8\xff\xe0" + b"jpeg-ish" * 8 + b"\xff\xd9"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("root")
    review = tmp_path_factory.mktemp("review")  # deliberately NOT under root: thumbs must come via /thumbs/
    (review / "events.json").write_text(json.dumps(
        {"schema": 1, "root": str(root), "generated": "2026-10-11T10:00:00Z", "events": [EVENT, OTHER]}))
    (review / "thumbs").mkdir()
    (review / EVENT["thumb"]).write_bytes(THUMB)
    media = root / EVENT["dir"]
    media.mkdir(parents=True)
    (media / "a-front.mp4").write_bytes(BLOB)
    (root / "legacy" / "thumbs").mkdir(parents=True)
    (root / "legacy" / "thumbs" / "old.jpg").write_bytes(THUMB)  # pre-contract events.json pointed under root
    (root / "secret.txt").write_bytes(b"inside root but fine")
    (root.parent / "outside.txt").write_bytes(b"must not be served")
    srv = vs.make_server(root, review, port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield {"root": root, "review": review, "base": base}
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


def test_index_served(site):
    code, hdr, body = call(site["base"], "/")
    assert code == 200
    assert hdr["Content-Type"].startswith("text/html")
    assert b"Tesla Dashcam" in body and b"/api/decisions" in body


def test_events(site):
    code, hdr, body = call(site["base"], "/api/events")
    assert code == 200
    data = json.loads(body)
    assert data["schema"] == 1 and [e["id"] for e in data["events"]] == [EVENT_ID, OTHER_ID]


def test_decisions_defaults(site):
    code, _, body = call(site["base"], "/api/decisions")
    assert code == 200
    dec = json.loads(body)
    assert set(dec) == {EVENT_ID, OTHER_ID}
    assert dec[EVENT_ID] == vs.DEFAULT_DECISION
    assert not list(site["review"].glob("decisions-*.json")), "GET must not create files"


def test_post_merge_and_atomic_file(site):
    base, review = site["base"], site["review"]
    code, _, body = call(base, "/api/decisions", "POST", {"id": EVENT_ID, "keep": "keep", "tags": ["#Crash", "crash", " near-miss "]})
    assert code == 200, body
    dec = json.loads(body)
    assert dec["keep"] == "keep" and dec["tags"] == ["crash", "near-miss"]
    assert dec["updated"].endswith("Z") and dec["cameras"] is None and dec["in_s"] is None
    # second partial write merges, keeps the earlier fields
    code, _, body = call(base, "/api/decisions", "POST", {"id": EVENT_ID, "in_s": 30.0, "cameras": ["back"], "note": " hit a cone "})
    assert code == 200, body
    dec = json.loads(body)
    assert dec["keep"] == "keep" and dec["tags"] == ["crash", "near-miss"]
    assert dec["in_s"] == 30.0 and dec["cameras"] == ["back"] and dec["note"] == "hit a cone"
    # on disk: one file per dump, no tmp left behind, proper envelope
    path = review / "decisions-DumpA.json"
    assert path.is_file() and not path.with_name(path.name + ".tmp").exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["schema"] == 1 and on_disk["dump"] == "DumpA"
    assert on_disk["events"][EVENT_ID] == dec
    assert not (review / "decisions-DumpB.json").exists()
    # merged view
    code, _, body = call(base, "/api/decisions")
    merged = json.loads(body)
    assert merged[EVENT_ID] == dec and merged[OTHER_ID] == vs.DEFAULT_DECISION
    # other dump lands in its own file
    code, _, body = call(base, "/api/decisions", "POST", {"id": OTHER_ID, "keep": "drop"})
    assert code == 200 and (review / "decisions-DumpB.json").is_file()
    assert json.loads(call(base, "/api/decisions")[2])[OTHER_ID]["keep"] == "drop"


@pytest.mark.parametrize("body, msg", [
    ({"id": EVENT_ID, "bogus": 1}, "unknown field"),
    ({"id": EVENT_ID, "keep": "maybe"}, "keep must be"),
    ({"id": EVENT_ID, "cameras": ["left_pillar"]}, "not present"),
    ({"id": EVENT_ID, "in_s": "12"}, "must be null or a number"),
    ({"id": EVENT_ID, "out_s": 500}, "beyond the event end"),
    ({"id": EVENT_ID, "in_s": 90, "out_s": 80}, "in_s must be before out_s"),
    ({"id": EVENT_ID, "precise": "yes"}, "must be a boolean"),
    ({"keep": "keep"}, "missing id"),
    ({"id": "../x|y", "keep": "keep"}, "bad event id"),
])
def test_post_validation(site, body, msg):
    code, _, raw = call(site["base"], "/api/decisions", "POST", body)
    assert code == 400, raw
    assert msg in json.loads(raw)["error"]


def test_post_bad_json(site):
    req = urllib.request.Request(site["base"] + "/api/decisions", data=b"{not json", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 400


def test_media_full_and_range(site):
    url = "/media/" + EVENT["dir"] + "/a-front.mp4"
    code, hdr, body = call(site["base"], url)
    assert code == 200 and body == BLOB and hdr["Accept-Ranges"] == "bytes"
    assert hdr["Content-Type"] == "video/mp4"
    code, hdr, body = call(site["base"], url, headers={"Range": "bytes=100-199"})
    assert code == 206
    assert hdr["Content-Range"] == f"bytes 100-199/{len(BLOB)}" and hdr["Content-Length"] == "100"
    assert body == BLOB[100:200]
    code, hdr, body = call(site["base"], url, headers={"Range": "bytes=1000-"})
    assert code == 206 and body == BLOB[1000:] and hdr["Content-Range"] == f"bytes 1000-1023/{len(BLOB)}"
    code, hdr, body = call(site["base"], url, headers={"Range": "bytes=-24"})
    assert code == 206 and body == BLOB[-24:]
    code, hdr, body = call(site["base"], url, headers={"Range": "bytes=5000-6000"})
    assert code == 416 and hdr["Content-Range"] == f"bytes */{len(BLOB)}"
    # HEAD carries the headers, no body
    code, hdr, body = call(site["base"], url, method="HEAD", headers={"Range": "bytes=0-9"})
    assert code == 206 and body == b"" and hdr["Content-Length"] == "10"


def test_thumbs_route(site):
    base = site["base"]
    # events.json thumb = "thumbs/<slug>.jpg" relative to the review dir -> GET /thumbs/<slug>.jpg
    code, hdr, body = call(base, "/" + EVENT["thumb"])
    assert code == 200 and body == THUMB and hdr["Content-Type"] == "image/jpeg"
    assert "max-age" in hdr.get("Cache-Control", "")
    # the review dir is outside root here, so the old /media/<thumb> spelling must NOT find it
    assert call(base, "/media/_review/" + EVENT["thumb"])[0] == 404
    assert call(base, "/media/" + EVENT["thumb"])[0] == 404
    # legacy events.json (thumb under the dump root) still works through /media/
    code, _, body = call(base, "/media/legacy/thumbs/old.jpg")
    assert code == 200 and body == THUMB
    # escapes out of <review_dir>/thumbs are rejected: events.json sits one level up
    for path in ("/thumbs/../events.json", "/thumbs/%2e%2e/events.json", "/thumbs/..%2fevents.json", "/thumbs/", "/thumbs/nope.jpg"):
        code, _, body = call(base, path)
        assert code == 404, (path, body)
        assert b"schema" not in body


def test_media_directory_404(site):
    code, _, _ = call(site["base"], "/media/" + EVENT["dir"])  # a directory
    assert code == 404


@pytest.mark.parametrize("path", [
    "/media/../outside.txt",
    "/media/DumpA/../../outside.txt",
    "/media/%2e%2e/outside.txt",
    "/media/..%2foutside.txt",
    "/media//etc/passwd",
    "/media/%2fetc/passwd",
])
def test_media_escape_rejected(site, path):
    code, _, body = call(site["base"], path)
    assert code == 404, (path, body)
    assert b"must not be served" not in body


def test_media_escape_via_symlink(site):
    link = site["root"] / "link.txt"
    try:
        os.symlink(site["root"].parent / "outside.txt", link)
    except OSError:
        pytest.skip("no symlinks")
    code, _, body = call(site["base"], "/media/link.txt")
    assert code == 404 and b"must not be served" not in body


def test_unknown_routes(site):
    assert call(site["base"], "/api/nope")[0] == 404
    assert call(site["base"], "/server.py")[0] == 404  # never leak the module source
    assert call(site["base"], "/nope", "POST", {"x": 1})[0] == 404


def test_missing_events_file(tmp_path):
    (tmp_path / "root").mkdir()
    srv = vs.make_server(tmp_path / "root", tmp_path / "review", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        code, _, body = call(base, "/api/events")
        assert code == 404 and "teslacam scan" in json.loads(body)["error"]
        code, _, body = call(base, "/api/decisions")
        assert code == 200 and json.loads(body) == {}
        # a decision for an unknown event is still accepted (loosely validated) and filed by dump
        code, _, body = call(base, "/api/decisions", "POST", {"id": "DumpZ|SavedClips|x", "keep": "drop"})
        assert code == 200 and (tmp_path / "review" / "decisions-DumpZ.json").is_file()
    finally:
        srv.shutdown()
        srv.server_close()


def test_hand_set_location_round_trips(site):
    base = site["base"]
    code, _, body = call(base, "/api/decisions", "POST", {"id": OTHER_ID, "lat": 55.65, "lon": -3.19, "city": " Peebles "})
    assert code == 200 and json.loads(body)["lat"] == 55.65 and json.loads(body)["city"] == "Peebles"
    code, _, body = call(base, "/api/decisions")
    assert json.loads(body)[OTHER_ID]["lon"] == -3.19
    code, _, body = call(base, "/api/decisions", "POST", {"id": OTHER_ID, "lat": 1.0})
    assert code == 400 and "together" in json.loads(body)["error"]
    code, _, body = call(base, "/api/decisions", "POST", {"id": OTHER_ID, "lat": 91.0, "lon": 0.0})
    assert code == 400
    code, _, body = call(base, "/api/decisions", "POST", {"id": OTHER_ID, "lat": None, "lon": None})
    assert code == 200 and json.loads(body)["lat"] is None and json.loads(body)["city"] == ""


def test_connection_burst_all_accepted(site):
    """Six <video> tags plus thumbnails connect in one burst. With Python's default
    backlog of 5 (< 3.13) macOS resets the overflow, and a reset media request leaves
    the player stalled — the listen queue must be deep enough for a burst."""
    import socket
    from urllib.parse import urlsplit

    port = urlsplit(site["base"]).port
    results: list[str] = []
    go = threading.Event()

    def one():
        s = socket.socket()
        s.settimeout(5)
        go.wait()
        try:
            s.connect(("127.0.0.1", port))
            s.sendall(b"GET /api/decisions HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            results.append("ok" if s.recv(64).startswith(b"HTTP/1.1 200") else "bad")
        except OSError as e:
            results.append(type(e).__name__)
        finally:
            s.close()

    threads = [threading.Thread(target=one) for _ in range(32)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join()
    assert results.count("ok") == 32, results


def test_post_requires_json_content_type(site):
    req = urllib.request.Request(site["base"] + "/api/decisions", method="POST",
                                 data=json.dumps({"id": EVENT_ID, "keep": "keep"}).encode(),
                                 headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 415


def test_post_cross_origin_refused(site):
    code, _, _ = call(site["base"], "/api/decisions", "POST", {"id": EVENT_ID, "keep": "keep"},
                      headers={"Origin": "http://evil.example"})
    assert code == 403
