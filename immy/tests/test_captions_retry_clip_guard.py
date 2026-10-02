"""Task 10: caption retry/sanitising/prompt-hash, and the CLIP model guard."""

from __future__ import annotations

import io
import shutil
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from immy import captions
from immy import journal as journal_mod
from immy import process as process_mod
from immy.journal import Journal
from immy.pg import LibraryInfo

FIXTURES = Path(__file__).parent / "fixtures"
LIB = LibraryInfo(id="lib-1", owner_id="owner-1", container_root="/mnt/external/originals")


# --- retry ---------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _http_error(code: int, headers: dict | None = None):
    return urllib.error.HTTPError(
        "http://x/v1/chat/completions", code, "err", headers or {}, io.BytesIO(b"{}"))


def _post(effects, sleeps):
    with patch("immy.captions.urllib.request.urlopen", side_effect=effects):
        return captions._post_json(
            "http://x/v1/chat/completions", {}, api_key=None, timeout_s=1,
            sleep=sleeps.append)


def test_retries_503_then_succeeds():
    sleeps: list[float] = []
    out = _post([_http_error(503), _http_error(500), _Resp(b'{"ok": 1}')], sleeps)
    assert out == {"ok": 1}
    assert len(sleeps) == 2
    assert all(0 < s <= captions.BACKOFF_CAP_S for s in sleeps)


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("refused"), TimeoutError("t"), ConnectionResetError("r"),
])
def test_retries_transport_errors(exc):
    sleeps: list[float] = []
    assert _post([exc, _Resp(b"{}")], sleeps) == {}
    assert len(sleeps) == 1


def test_429_honours_retry_after():
    sleeps: list[float] = []
    _post([_http_error(429, {"Retry-After": "7"}), _Resp(b"{}")], sleeps)
    assert sleeps == [7.0]


def test_gives_up_after_four_attempts():
    sleeps: list[float] = []
    with patch("immy.captions.urllib.request.urlopen",
               side_effect=[_http_error(503)] * 10) as m:
        with pytest.raises(captions.CaptionError, match="after 4 attempts"):
            captions._post_json("http://x", {}, api_key=None, timeout_s=1,
                                sleep=sleeps.append)
    assert m.call_count == 4
    assert len(sleeps) == 3


def test_no_retry_on_other_4xx():
    sleeps: list[float] = []
    with patch("immy.captions.urllib.request.urlopen",
               side_effect=[_http_error(400), _Resp(b"{}")]) as m:
        with pytest.raises(captions.CaptionError, match="HTTP 400"):
            captions._post_json("http://x", {}, api_key=None, timeout_s=1,
                                sleep=sleeps.append)
    assert m.call_count == 1 and sleeps == []


# --- sanitising ----------------------------------------------------------


def _caption_with(content: str, tmp_path: Path):
    from PIL import Image
    src = tmp_path / "a.jpg"
    Image.new("RGB", (2, 2)).save(src, "JPEG")
    resp = {"model": "m", "choices": [{"message": {"content": content}}]}
    with patch.object(captions, "_post_json", return_value=resp):
        return captions.caption(src, config=captions.CaptionerConfig())


def test_strips_think_block_and_quotes(tmp_path):
    r = _caption_with('<think>hmm\nlet me see</think>\n"A dog on a hill."', tmp_path)
    assert r.text == "A dog on a hill."


def test_unclosed_think_is_rejected(tmp_path):
    with pytest.raises(captions.CaptionError, match="rejected"):
        _caption_with("<think>I am still thinking about", tmp_path)


@pytest.mark.parametrize("bad", [
    "I'm sorry, but I can't help with that.",
    "I cannot describe this image.",
    "As an AI language model I do not see",
    "Too short",
    "<think>x</think>",
])
def test_rejects_refusals_and_short(bad, tmp_path):
    with pytest.raises(captions.CaptionError):
        _caption_with(bad, tmp_path)


def test_legit_caption_with_i_not_refusal(tmp_path):
    assert _caption_with("Interior of a small cafe", tmp_path).text.startswith("Interior")


# --- prompt hash ---------------------------------------------------------


def test_prompt_hash_tracks_prompt_tokens_extra_body():
    base = captions.CaptionerConfig(prompt="p", max_tokens=10, extra_body={"a": 1, "b": 2})
    h = captions.prompt_hash(base)
    assert len(h) == 8
    assert h == captions.prompt_hash(
        captions.CaptionerConfig(prompt="p", max_tokens=10, extra_body={"b": 2, "a": 1}))
    for other in (
        captions.CaptionerConfig(prompt="q", max_tokens=10, extra_body={"a": 1, "b": 2}),
        captions.CaptionerConfig(prompt="p", max_tokens=11, extra_body={"a": 1, "b": 2}),
        captions.CaptionerConfig(prompt="p", max_tokens=10, extra_body={"a": 1}),
    ):
        assert captions.prompt_hash(other) != h


def test_caption_version_folds_hash():
    assert journal_mod.caption_version("m") == "caption:m"
    assert journal_mod.caption_version("m", "abcd1234") == "caption:m@abcd1234"


def test_caption_prompt_changed_rules():
    f = process_mod.caption_prompt_changed
    assert not f(None, "m", "aaaaaaaa")
    assert not f({"version": "caption:m"}, "m", "aaaaaaaa")  # legacy: matches
    assert not f({"version": "caption:m", "meta": {"text": "x"}}, "m", "aaaaaaaa")
    assert not f({"version": "caption:m@aaaaaaaa"}, "m", "aaaaaaaa")
    assert f({"version": "caption:m@bbbbbbbb"}, "m", "aaaaaaaa")
    assert f({"version": "caption:m", "meta": {"prompt_hash": "bbbbbbbb"}}, "m", "aaaaaaaa")
    assert not f({"version": "caption:other@bbbbbbbb"}, "m", "aaaaaaaa")  # model bump


def _caption_run(tmp_path, monkeypatch, entry_version, entry_meta_extra, cfg,
                 db_description="AI: old text"):
    from immy import exif as exif_mod
    from immy import offline as offline_mod
    from immy.derivatives import DerivativeFile, DerivativeResult
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    last = {"sql": ""}
    cur.execute.side_effect = lambda sql, params=None: last.__setitem__("sql", sql)
    # resumed run: the asset row already exists (else the journal is cleared)
    cur.fetchone.side_effect = lambda: (
        None if "INSERT INTO asset (" in last["sql"] else ("uuid-x",))
    conn.cursor.return_value = cur
    preview = tmp_path / "out" / "preview.jpeg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"fake")
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: DerivativeResult(files=[DerivativeFile(
            kind="preview", staged_path=preview, relative_path="thumbs/x/p.jpeg",
            is_progressive=True, is_transparent=False)], width=100, height=100),
    )
    calls = {"n": 0}

    def _fake(media, *, config, preview=None, context=None):
        calls["n"] += 1
        return MagicMock(text="fresh caption text", model=config.model,
                         prompt_tokens=1, completion_tokens=2)
    monkeypatch.setattr("immy.process.captions_mod.caption", _fake)
    monkeypatch.setattr(offline_mod.PgSink, "get_description", lambda self, a: db_description)
    rows = exif_mod.read_folder(target)
    asset, _ = process_mod.build_rows(rows[0].path, target, rows[0], LIB)
    cs = asset.checksum.hex()
    j = Journal.load(target)
    j.mark_done(cs, "ingest", "v1", meta={"asset_id": "uuid-x"})
    j.mark_done(cs, "caption", entry_version,
                meta={"text": "old text", "model": cfg.model, **entry_meta_extra})
    j.flush()
    res = process_mod.process_trip(
        target, conn, LIB, compute_derivatives=True, compute_captions=True,
        captioner_config=cfg)
    return calls["n"], res[0]


CFG = captions.CaptionerConfig(endpoint="http://x", model="g", prompt="new prompt", max_tokens=64)


def test_prompt_change_recaptions_even_with_ai_description(tmp_path, monkeypatch):
    n, _ = _caption_run(tmp_path, monkeypatch, "caption:g@00000000", {}, CFG)
    assert n == 1


def test_legacy_entry_without_hash_is_not_recaptioned(tmp_path, monkeypatch):
    # no DB AI-prefix fallback: only the journal can explain "not recaptioned"
    n, res = _caption_run(tmp_path, monkeypatch, "caption:g", {}, CFG,
                          db_description=None)
    assert n == 0
    assert res.caption["text"] == "old text"


def test_same_hash_is_cached(tmp_path, monkeypatch):
    v = journal_mod.caption_version("g", captions.prompt_hash(CFG))
    n, _ = _caption_run(tmp_path, monkeypatch, v, {}, CFG)
    assert n == 0


def test_marker_caption_step_includes_hash():
    steps = process_mod.marker_steps(compute_captions=True, captioner_config=CFG)
    assert steps["caption"] == journal_mod.caption_version("g", captions.prompt_hash(CFG))


# --- CLIP guard ----------------------------------------------------------


def test_guard_reason_matrix():
    g = process_mod.clip_guard_reason
    ok = dict(clip_model="ViT-B-32__openai", clip_backend="onnx",
              allow_mlx_clip=False, immich_model="ViT-B-32__openai")
    assert g(**ok) is None
    assert "mismatch" in (g(**{**ok, "immich_model": "ViT-L-14__openai"}) or "") or \
        "!=" in g(**{**ok, "immich_model": "ViT-L-14__openai"})
    assert "mlx" in g(**{**ok, "clip_backend": "mlx"})
    assert g(**{**ok, "clip_backend": "mlx", "allow_mlx_clip": True}) is None
    assert g(**{**ok, "immich_model": None}) is None  # offline: unknown, unchecked


def test_fetch_immich_clip_model_default_and_explicit():
    from immy import pg
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    assert pg.fetch_immich_clip_model(conn) == pg.IMMICH_DEFAULT_CLIP_MODEL == "ViT-B-32__openai"
    conn.execute.return_value.fetchone.return_value = (None,)
    assert pg.fetch_immich_clip_model(conn) == "ViT-B-32__openai"
    conn.execute.return_value.fetchone.return_value = ("ViT-L-14__openai",)
    assert pg.fetch_immich_clip_model(conn) == "ViT-L-14__openai"


def _clip_run(tmp_path, monkeypatch, *, immich_model, backend, allow):
    from immy.derivatives import DerivativeFile, DerivativeResult
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur
    preview = tmp_path / "out" / "p.jpeg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"fake")
    monkeypatch.setattr("immy.process.pg_mod.fetch_smart_search_dim", lambda c: 4)
    monkeypatch.setattr("immy.offline.pg_mod.fetch_immich_clip_model", lambda c: immich_model)
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: DerivativeResult(files=[DerivativeFile(
            kind="preview", staged_path=preview, relative_path="thumbs/x/p.jpeg",
            is_progressive=True, is_transparent=False)], width=100, height=100))
    monkeypatch.setattr("immy.process.clip_mod.embed", lambda *a, **k: [0.1, 0.2, 0.3, 0.4])
    msgs: list[str] = []
    res = process_mod.process_trip(
        target, conn, LIB, compute_derivatives=True, compute_clip=True,
        clip_backend=backend, allow_mlx_clip=allow, progress=msgs.append)
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    return res[0], sqls, msgs


def test_clip_model_mismatch_refuses_write(tmp_path, monkeypatch):
    res, sqls, msgs = _clip_run(tmp_path, monkeypatch, immich_model="ViT-L-14__openai",
                                backend="onnx", allow=False)
    assert res.clip_embedded is False
    assert not any("smart_search" in s for s in sqls)
    assert any("CLIP disabled" in m and "ViT-L-14__openai" in m for m in msgs)


def test_mlx_refused_unless_allowed(tmp_path, monkeypatch):
    res, sqls, _ = _clip_run(tmp_path, monkeypatch, immich_model="ViT-B-32__openai",
                             backend="mlx", allow=False)
    assert res.clip_embedded is False
    assert not any("INSERT INTO smart_search" in s for s in sqls)


def test_mlx_allowed_writes(tmp_path, monkeypatch):
    res, sqls, _ = _clip_run(tmp_path, monkeypatch, immich_model="ViT-B-32__openai",
                             backend="mlx", allow=True)
    assert res.clip_embedded is True
    assert any("INSERT INTO smart_search" in s for s in sqls)


def test_config_allow_mlx_clip_parsed(tmp_path):
    from immy import config
    p = tmp_path / "c.yml"
    p.write_text("ml:\n  allow_mlx_clip: true\n", encoding="utf-8")
    assert config.load(p).ml.allow_mlx_clip is True


def test_process_trip_omitting_allow_mlx_clip_refuses_mlx(tmp_path, monkeypatch):
    from immy.derivatives import DerivativeFile, DerivativeResult
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur
    preview = tmp_path / "p.jpeg"
    preview.write_bytes(b"fake")
    monkeypatch.setattr("immy.process.pg_mod.fetch_smart_search_dim", lambda c: 4)
    monkeypatch.setattr("immy.offline.pg_mod.fetch_immich_clip_model",
                        lambda c: "ViT-B-32__openai")
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: DerivativeResult(files=[DerivativeFile(
            kind="preview", staged_path=preview, relative_path="t/p.jpeg",
            is_progressive=True, is_transparent=False)], width=1, height=1))
    monkeypatch.setattr("immy.process.clip_mod.embed", lambda *a, **k: [0.1, 0.2, 0.3, 0.4])
    res = process_mod.process_trip(target, conn, LIB, compute_derivatives=True,
                                   compute_clip=True)  # backend defaults to mlx
    assert res[0].clip_embedded is False
    assert not any("INSERT INTO smart_search" in c.args[0] for c in cur.execute.call_args_list)


# --- Retry-After ---------------------------------------------------------


def test_retry_after_http_date_honoured():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=40), usegmt=True)
    sleeps: list[float] = []
    _post([_http_error(503, {"Retry-After": when}), _Resp(b"{}")], sleeps)
    assert len(sleeps) == 1 and 35 <= sleeps[0] <= 40


def test_retry_after_over_cap_fails_without_sleeping():
    sleeps: list[float] = []
    with patch("immy.captions.urllib.request.urlopen",
               side_effect=[_http_error(429, {"Retry-After": "600"}), _Resp(b"{}")]) as m:
        with pytest.raises(captions.CaptionError, match="exceeds"):
            captions._post_json("http://x", {}, api_key=None, timeout_s=1,
                                sleep=sleeps.append)
    assert m.call_count == 1 and sleeps == []


def test_retry_after_between_backoff_and_cap_not_shortened():
    sleeps: list[float] = []
    _post([_http_error(429, {"Retry-After": "90"}), _Resp(b"{}")], sleeps)
    assert sleeps == [90.0]


# --- offline caption cache + deferred CLIP replay ------------------------


def test_offline_prior_caption_with_other_hash_is_regenerated(tmp_path, monkeypatch):
    from immy import offline as offline_mod
    from immy.derivatives import DerivativeFile, DerivativeResult
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    preview = tmp_path / "p.jpeg"
    preview.write_bytes(b"fake")
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: DerivativeResult(files=[DerivativeFile(
            kind="preview", staged_path=preview, relative_path="t/p.jpeg",
            is_progressive=True, is_transparent=False)], width=1, height=1))
    calls = {"n": 0}

    def _fake(media, *, config, preview=None, context=None):
        calls["n"] += 1
        return MagicMock(text="brand new caption text", model=config.model,
                         prompt_tokens=1, completion_tokens=1)
    monkeypatch.setattr("immy.process.captions_mod.caption", _fake)
    root = tmp_path / "off"
    sink = offline_mod.OfflineSink(target, LIB, offline_root=root, clip_dim=4)
    kw = dict(compute_derivatives=True, compute_captions=True, sink=sink)
    process_mod.process_trip(target, None, LIB, captioner_config=CFG, **kw)
    assert calls["n"] == 1
    # same prompt: offline cache reused
    process_mod.process_trip(target, None, LIB, captioner_config=CFG, **kw)
    assert calls["n"] == 1
    # prompt changed: regenerated and stored under the new hash
    cfg2 = captions.CaptionerConfig(endpoint="http://x", model="g", prompt="other", max_tokens=64)
    process_mod.process_trip(target, None, LIB, captioner_config=cfg2, **kw)
    assert calls["n"] == 2
    entry = next(iter(offline_mod.iter_entries(target, offline_root=root)))[1]
    assert entry["caption"]["prompt_hash"] == captions.prompt_hash(cfg2)


def _sync_with_clip(tmp_path, monkeypatch, provenance, immich_model):
    import numpy as np
    from immy import offline as offline_mod
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    root = tmp_path / "off"
    sink = offline_mod.OfflineSink(target, LIB, offline_root=root, clip_dim=4)
    res = process_mod.process_trip(target, None, LIB, sink=sink)
    if provenance is not None:
        sink.set_clip_provenance(**provenance)
    sink.upsert_clip(res[0].asset_id, [0.1, 0.2, 0.3, 0.4], "[...]")
    upserts: list[str] = []
    monkeypatch.setattr(offline_mod.pg_mod, "upsert_smart_search",
                        lambda c, a, l: upserts.append(a))
    monkeypatch.setattr(offline_mod.pg_mod, "fetch_immich_clip_model", lambda c: immich_model)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("replayed-uuid",)
    conn.cursor.return_value = cur
    summary = offline_mod.sync_trip(target, conn, library=LIB, offline_root=root)
    return summary, upserts


OK_PROV = dict(model="ViT-B-32__openai", backend="onnx", allow_mlx=False)


def test_sync_writes_clip_when_provenance_matches(tmp_path, monkeypatch):
    summary, ups = _sync_with_clip(tmp_path, monkeypatch, OK_PROV, "ViT-B-32__openai")
    assert ups == ["replayed-uuid"] and summary["clip_refused"] == 0


def test_sync_refuses_equal_dim_model_mismatch(tmp_path, monkeypatch):
    # ViT-B-16 is also 512-dim: dimension alone can't catch this.
    summary, ups = _sync_with_clip(tmp_path, monkeypatch,
                                   {**OK_PROV, "model": "ViT-B-16__openai"},
                                   "ViT-B-32__openai")
    assert ups == [] and summary["clip_refused"] == 1 and summary["synced"] == 1


def test_sync_refuses_legacy_payload_without_provenance(tmp_path, monkeypatch):
    summary, ups = _sync_with_clip(tmp_path, monkeypatch, None, "ViT-B-32__openai")
    assert ups == [] and summary["clip_refused"] == 1 and summary["synced"] == 1


def test_sync_refuses_mlx_not_allowed_but_accepts_allowed(tmp_path, monkeypatch):
    s, ups = _sync_with_clip(tmp_path, monkeypatch,
                             {**OK_PROV, "backend": "mlx"}, "ViT-B-32__openai")
    assert ups == [] and s["clip_refused"] == 1


def test_sync_accepts_mlx_when_allowed_at_generation(tmp_path, monkeypatch):
    s, ups = _sync_with_clip(tmp_path, monkeypatch,
                             {**OK_PROV, "backend": "mlx", "allow_mlx": True},
                             "ViT-B-32__openai")
    assert ups == ["replayed-uuid"] and s["clip_refused"] == 0
