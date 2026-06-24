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
    assert "x-api-key: KEY" in remote
    assert "-X GET" in remote
    # GET has no body → nothing piped, no Content-Type.
    assert cap["input"] is None
    assert "Content-Type" not in remote


def test_ssh_post_pipes_body_over_stdin(monkeypatch):
    cap = {}
    monkeypatch.setattr(subprocess, "run", _fake_run(b"\n200", capture=cap))
    out = _client()._request("PUT", "/api/jobs/smartSearch",
                             body={"command": "start", "force": True})
    assert out is None  # empty body → None, like urllib path
    remote = cap["cmd"][-1]
    assert "--data-binary @-" in remote
    assert "Content-Type: application/json" in remote
    # JSON goes over stdin, never into the shell-quoted command.
    assert cap["input"] == b'{"command": "start", "force": true}'
    assert "force" not in remote


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
