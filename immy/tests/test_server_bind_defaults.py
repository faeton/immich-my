"""Security: review/pano servers bind loopback by default and warn when
exposed (they have no authentication)."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from immy import cli

runner = CliRunner()

CASES = [
    (["dedup", "review-server"], "immy.dedup.review", "serve"),
    (["triage", "review-server"], "immy.triage.review", "serve"),
    (["pano-server"], "immy.pano", "serve"),
]


def _run(monkeypatch, tmp_path, args, mod, extra):
    import importlib
    seen = {}
    m = importlib.import_module(mod)
    monkeypatch.setattr(m, "serve", lambda *a: seen.setdefault("args", a))
    res = runner.invoke(
        cli.app, [*args, "--manifest", str(tmp_path / "m.sqlite"), *extra]
    )
    assert res.exit_code == 0, res.output
    return seen["args"], res.output


@pytest.mark.parametrize("args,mod,fn", CASES)
def test_default_host_is_loopback_no_warning(monkeypatch, tmp_path, args, mod, fn):
    a, out = _run(monkeypatch, tmp_path, args, mod, [])
    assert a[-2] == "127.0.0.1"
    assert "no authentication" not in out


@pytest.mark.parametrize("args,mod,fn", CASES)
def test_explicit_public_host_warns(monkeypatch, tmp_path, args, mod, fn):
    a, out = _run(monkeypatch, tmp_path, args, mod, ["--host", "0.0.0.0"])
    assert a[-2] == "0.0.0.0"
    assert "no authentication" in out
