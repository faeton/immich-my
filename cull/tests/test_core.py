"""cull.core: Range responses, escapes, POST guard, host resolution, burst."""

from __future__ import annotations

import functools
import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cull.core import host as host_mod
from cull.core.http import ApiError, MediaHandler, QuietServer, resolve_under
from cull.core.jsonio import read_json, write_json_atomic

BLOB = bytes(range(256)) * 40  # 10240 bytes


class EchoHandler(MediaHandler):
    def __init__(self, *a, base: Path, **kw):
        self.base = base
        super().__init__(*a, **kw)

    def do_GET(self):
        full = self._resolve_under(self.base, self.path.lstrip("/"))
        if full is None:
            return self._send_error_json(404, "not found")
        self._send_file(full)

    def do_POST(self):
        try:
            self._send_json(200, {"got": self._read_json_body()})
        except ApiError as e:
            self._send_error_json(e.code, e.message)


@pytest.fixture
def site(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "clip.mp4").write_bytes(BLOB)
    (base / "x.LRF").write_bytes(BLOB)
    (tmp_path / "outside.txt").write_text("no")
    srv = QuietServer(("127.0.0.1", 0), functools.partial(EchoHandler, base=base))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_full_and_ranges(site):
    code, hdr, body = call(site + "/clip.mp4")
    assert code == 200 and body == BLOB and hdr["Accept-Ranges"] == "bytes"
    code, hdr, body = call(site + "/clip.mp4", headers={"Range": "bytes=10-19"})
    assert code == 206 and body == BLOB[10:20] and hdr["Content-Range"] == f"bytes 10-19/{len(BLOB)}"
    code, _, body = call(site + "/clip.mp4", headers={"Range": "bytes=-5"})
    assert code == 206 and body == BLOB[-5:]
    code, hdr, _ = call(site + "/clip.mp4", headers={"Range": f"bytes={len(BLOB)}-"})
    assert code == 416 and hdr["Content-Range"] == f"bytes */{len(BLOB)}"


def test_lrf_is_served_as_mp4(site):
    code, hdr, _ = call(site + "/x.LRF")
    assert code == 200 and hdr["Content-Type"] == "video/mp4"


def test_escapes_rejected(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "a b+'#.mp4").write_bytes(b"x")
    (tmp_path / "outside.txt").write_text("no")
    assert resolve_under(base, "a%20b%2B%27%23.mp4") == (base / "a b+'#.mp4").resolve()
    for rel in ("../outside.txt", "%2e%2e/outside.txt", "/etc/passwd", "a\x00b", "", "."):
        assert resolve_under(base, rel) is None


def test_post_guard(site):
    body = json.dumps({"a": 1}).encode()
    assert call(site + "/", "POST", body, {"Content-Type": "application/json"})[0] == 200
    assert call(site + "/", "POST", body, {"Content-Type": "text/plain"})[0] == 415
    host = site.removeprefix("http://")
    ok = {"Content-Type": "application/json", "Origin": f"http://{host}"}
    assert call(site + "/", "POST", body, ok)[0] == 200
    bad = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert call(site + "/", "POST", body, bad)[0] == 403
    assert call(site + "/", "POST", b"{nope", {"Content-Type": "application/json"})[0] == 400


def test_burst_of_connections(site):
    port = int(site.rsplit(":", 1)[1])
    results: list[str] = []
    go = threading.Event()

    def one():
        s = socket.socket()
        s.settimeout(5)
        go.wait()
        try:
            s.connect(("127.0.0.1", port))
            s.sendall(b"GET /clip.mp4 HTTP/1.1\r\nHost: x\r\nRange: bytes=0-0\r\nConnection: close\r\n\r\n")
            results.append("ok" if s.recv(64).startswith(b"HTTP/1.1 206") else "bad")
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


def test_json_atomic(tmp_path):
    p = tmp_path / "d" / "x.json"
    assert read_json(p, {"empty": True}) == {"empty": True}
    write_json_atomic(p, {"b": 1, "a": "é"})
    assert read_json(p) == {"a": "é", "b": 1}
    assert not list(p.parent.glob("*.tmp"))


IFACES = [("lo", "127.0.0.1"), ("docker0", "100.70.0.1"), ("tailscale0", "100.115.236.50"),
          ("enp196s0", "192.168.18.137")]


def test_host_tailscale_picks_the_tailscale_interface():
    assert host_mod.resolve_host("tailscale", ifaces=IFACES, cli=lambda: None) == "100.115.236.50"


def test_host_tailscale_errors():
    with pytest.raises(host_mod.HostError, match="none"):
        host_mod.resolve_host("tailscale", ifaces=[("docker0", "100.70.0.1")], cli=lambda: None)
    two = IFACES + [("tailscale1", "100.101.1.1")]
    with pytest.raises(host_mod.HostError, match="100.101.1.1"):
        host_mod.resolve_host("tailscale", ifaces=two, cli=lambda: None)


def test_host_tailscale_macos_cli_fallback():
    assert host_mod.resolve_host("tailscale", ifaces=[("utun4", "100.80.1.2")], cli=lambda: "100.80.1.2") == "100.80.1.2"


def test_host_refuses_every_interface():
    for spec in ("0.0.0.0", "::", ""):
        with pytest.raises(host_mod.HostError):
            host_mod.resolve_host(spec, ifaces=IFACES, cli=lambda: None)
    assert host_mod.resolve_host("127.0.0.1", ifaces=IFACES, cli=lambda: None) == "127.0.0.1"


def test_interfaces_parses_ip_output(monkeypatch):
    out = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
           "31: tailscale0    inet 100.115.236.50/32 scope global tailscale0\\       valid_lft forever\n")
    monkeypatch.setattr(host_mod.shutil, "which", lambda n: "/usr/sbin/ip" if n == "ip" else None)
    monkeypatch.setattr(host_mod, "_run", lambda cmd: out)
    assert host_mod.interfaces() == [("lo", "127.0.0.1"), ("tailscale0", "100.115.236.50")]


def test_interfaces_parses_macos_ifconfig(monkeypatch):
    out = ("lo0: flags=8049<UP> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n"
           "utun4: flags=8051<UP> mtu 1280\n\tinet 100.80.1.2 --> 100.80.1.2 netmask 0xffffffff\n")
    monkeypatch.setattr(host_mod.shutil, "which", lambda n: None)
    monkeypatch.setattr(host_mod, "_run", lambda cmd: out)
    assert host_mod.interfaces() == [("lo0", "127.0.0.1"), ("utun4", "100.80.1.2")]
