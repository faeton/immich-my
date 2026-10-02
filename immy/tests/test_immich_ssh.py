"""ssh-curl transport for ImmichClient — n5's API is localhost-bound + no port
forwarding, so requests run `curl` ON n5 over ssh. These assert the remote
command shape, status parsing, body-over-stdin, and error mirroring without
ever touching the network (subprocess.run is faked)."""
from __future__ import annotations

import subprocess
import types

import pytest

from immy.immich import ImmichClient, ImmichError


def _client():
    return ImmichClient(url="http://127.0.0.1:2283", api_key="KEY", ssh_host="n5")


def _fake_run(stdout=b"", returncode=0, stderr=b"", capture=None):
    def run(cmd, input=None, capture_output=False, timeout=None):
        if capture is not None:
            capture["cmd"] = cmd
            capture["input"] = input
            capture["timeout"] = timeout
        return types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr,
        )
    return run


def test_ssh_used_when_host_set(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(b'{"ok":true}\n200', capture=cap))
    out = _client()._request("GET", "/api/jobs")
    assert out == {"ok": True}
    # remote command runs over ssh to the configured host, BatchMode on.
    cmd = cap["cmd"]
    assert cmd[0] == "ssh" and "n5" in cmd
    assert "BatchMode=yes" in cmd
    remote = cmd[-1]
    assert remote.startswith("curl ")
    assert "http://127.0.0.1:2283/api/jobs" in remote
    assert "-X GET" in remote
    # key travels on stdin as a curl config, never in any argv.
    assert "-K -" in remote
    assert cap["input"] == b'header = "x-api-key: KEY"\n'
    assert "Content-Type" not in remote


def test_ssh_post_pipes_body_over_stdin(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(b"\n200", capture=cap))
    out = _client()._request("PUT", "/api/jobs/smartSearch",
                             body={"command": "start", "force": True})
    assert out is None  # empty body → None, like urllib path
    remote = cap["cmd"][-1]
    assert "Content-Type: application/json" in remote
    # JSON + key go over stdin (one curl config), never into any argv.
    assert cap["input"] == (
        b'header = "x-api-key: KEY"\n'
        b'data-binary = "{\\"command\\": \\"start\\", \\"force\\": true}"\n'
    )
    assert "force" not in remote


def test_ssh_api_key_never_in_argv(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(b"\n200", capture=cap))
    key = 'sec"ret\\key'
    c = ImmichClient(url="http://127.0.0.1:2283", api_key=key, ssh_host="n5")
    c._request("POST", "/api/x", body={"a": 1})
    assert not any("sec" in a for a in cap["cmd"])
    assert b'header = "x-api-key: sec\\"ret\\\\key"\n' in cap["input"]


def test_ssh_http_error_mirrors_request(monkeypatch):
    # curl exits 0 on HTTP 4xx (no -f); the body+code carry the failure.
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(b'{"error":"bad"}\n400'))
    with pytest.raises(ImmichError) as e:
        _client()._request("GET", "/api/jobs")
    assert "→ 400" in str(e.value) and "bad" in str(e.value)


def test_ssh_transport_failure_raises(monkeypatch):
    # ssh itself failing (rc 255) is a transport error, not an HTTP status.
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(b"", returncode=255, stderr=b"Permission denied"))
    with pytest.raises(ImmichError) as e:
        _client()._request("GET", "/api/jobs")
    assert "transport" in str(e.value) and "255" in str(e.value)


def test_ssh_timeout_raises(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=25)
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ImmichError) as e:
        _client()._request("GET", "/api/jobs")
    assert "timeout" in str(e.value)


def test_no_ssh_host_keeps_urllib_path(monkeypatch):
    # Without ssh_host, subprocess must NOT be touched — direct urllib path.
    def forbidden(*a, **k):
        raise AssertionError("subprocess.run must not run on the urllib path")
    monkeypatch.setattr(subprocess, "run", forbidden)
    calls = []
    monkeypatch.setattr(
        ImmichClient, "_request_ssh",
        lambda self, *a, **k: (_ for _ in ()).throw(
            AssertionError("ssh path must not run without ssh_host")),
    )
    # Stub the urllib opener so the call resolves without a socket.
    import immy.immich as mod

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp())
    c = ImmichClient(url="http://x", api_key="k")  # no ssh_host
    assert c._request("GET", "/api/jobs") == {}
