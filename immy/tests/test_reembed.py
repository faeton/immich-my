"""SmartSearch/FaceDetection re-embed trigger — client request shape + the
promote `_trigger_reembed` logic (immy-inserted assets aren't auto-queued, so
promote must fire these explicitly)."""
from __future__ import annotations

import pytest

from immy.immich import ImmichClient, ImmichError
from immy import promote as promote_mod


def test_queue_job_request_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ImmichClient, "_request",
        lambda self, method, path, body=None: calls.append((method, path, body)),
    )
    c = ImmichClient(url="http://x", api_key="k")
    c.queue_job("smartSearch", force=True)
    c.queue_job("faceDetection")  # default force=False
    assert calls == [
        ("PUT", "/api/jobs/smartSearch", {"command": "start", "force": True}),
        ("PUT", "/api/jobs/faceDetection", {"command": "start", "force": False}),
    ]


def test_get_jobs_returns_dict(monkeypatch):
    monkeypatch.setattr(ImmichClient, "_request", lambda *a, **k: None)
    assert ImmichClient(url="http://x", api_key="k").get_jobs() == {}


class _FakeClient:
    def __init__(self, *, fail: set[str] | None = None):
        self.queued: list[tuple[str, bool]] = []
        self.fail = fail or set()

    def get_jobs(self):
        return {"smartSearch": {"jobCounts": {"waiting": 3}},
                "faceDetection": {"jobCounts": {"waiting": 0}}}

    def queue_job(self, name, *, force=False):
        if name in self.fail:
            raise ImmichError(f"boom {name}")
        self.queued.append((name, force))


@pytest.mark.parametrize("mode,force", [("missing", False), ("all", True)])
def test_trigger_reembed_queues_both(mode, force):
    fc = _FakeClient()
    out = promote_mod._trigger_reembed(fc, mode)
    assert set(fc.queued) == {("smartSearch", force), ("faceDetection", force)}
    assert out["mode"] == mode and out["force"] is force
    assert out["smartSearch"] == "queued" and out["faceDetection"] == "queued"
    assert out["pending_before"]["smartSearch"] == {"waiting": 3}


def test_trigger_reembed_swallows_job_error():
    fc = _FakeClient(fail={"smartSearch"})
    out = promote_mod._trigger_reembed(fc, "missing")  # must not raise
    assert out["smartSearch"].startswith("error:")
    assert out["faceDetection"] == "queued"  # the other still fires


# --- tag API request shapes ------------------------------------------------

def test_upsert_tags_request_and_parse(monkeypatch):
    calls = []
    def fake_request(self, method, path, body=None):
        calls.append((method, path, body))
        return [{"id": "t1", "name": "post-edited"},
                {"id": "t2", "name": "with-anya"}]
    monkeypatch.setattr(ImmichClient, "_request", fake_request)
    c = ImmichClient(url="http://x", api_key="k")
    out = c.upsert_tags(["post-edited", "with-anya"])
    assert calls == [("PUT", "/api/tags", {"tags": ["post-edited", "with-anya"]})]
    assert out == {"post-edited": "t1", "with-anya": "t2"}


def test_upsert_tags_empty_is_noop(monkeypatch):
    monkeypatch.setattr(ImmichClient, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert ImmichClient(url="http://x", api_key="k").upsert_tags([]) == {}


def test_tag_assets_request_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ImmichClient, "_request",
        lambda self, method, path, body=None: calls.append((method, path, body)) or
        [{"id": i, "success": True} for i in body["ids"]],
    )
    c = ImmichClient(url="http://x", api_key="k")
    res = c.tag_assets("t1", ["a", "b"])
    assert calls == [("PUT", "/api/tags/t1/assets", {"ids": ["a", "b"]})]
    assert [r["success"] for r in res] == [True, True]
