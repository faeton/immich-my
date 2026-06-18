"""WhisperCppBackend (Phase 2) — unit tests with a mocked `/inference`.

No HTTP, no docker, no ffmpeg-on-real-media: the speech-plan and ffmpeg slice
are stubbed so these run anywhere. The HTTP/verbose_json parsing, language
name→code mapping, candidate clamp, and region-offset merge are what's under
test — the shared render/scrub/sidecar layer has its own coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from immy.asr import plan as plan_mod
from immy.asr import registry
from immy.asr.types import SpeechPlan, SpeechRegion
from immy.asr.whispercpp_backend import (
    WhisperCppBackend,
    WhisperCppError,
    _encode_multipart,
    _lang_name_to_code,
)


def _verbose_json(text: str, segs, language="english"):
    return {
        "language": language,
        "text": text,
        "segments": [
            {"start": s, "end": e, "text": t, "words": [], "tokens": []}
            for (s, e, t) in segs
        ],
    }


def test_endpoint_required():
    with pytest.raises(WhisperCppError):
        WhisperCppBackend(endpoint="")
    # registry surfaces the same error for a misconfigured NAS profile
    with pytest.raises(WhisperCppError):
        registry.get_backend("whispercpp", endpoint=None)


def test_lang_name_to_code():
    assert _lang_name_to_code("english") == "en"
    assert _lang_name_to_code("Russian") == "ru"
    assert _lang_name_to_code("ukrainian") == "uk"
    assert _lang_name_to_code("ru") == "ru"          # already a code
    assert _lang_name_to_code("klingon") == "klingon"  # unknown → clamp later
    assert _lang_name_to_code(None) is None


def test_encode_multipart_has_file_and_fields(tmp_path: Path):
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFFfake")
    body, ctype = _encode_multipart(
        {"response_format": "verbose_json", "language": "auto"}, wav)
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="response_format"' in body
    assert b"verbose_json" in body
    assert b'name="file"; filename="x.wav"' in body
    assert b"RIFFfake" in body


def test_whole_file_transcribe(tmp_path: Path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00")
    be = WhisperCppBackend(endpoint="http://n5:8090")

    # No regions → whole-file path. Stub the plan + wav decode + HTTP.
    monkeypatch.setattr(
        plan_mod, "build_speech_plan",
        lambda m, **k: SpeechPlan(
            media=m, duration_s=10.0, regions=(), lang_candidates=(), initial_prompt=None),
    )
    monkeypatch.setattr(
        plan_mod, "materialize_wav",
        lambda m, dst, **k: (dst.write_bytes(b"w"), dst)[1],
    )
    captured = {}

    def fake_infer(self, wav, *, language, prompt):
        captured["language"] = language
        captured["prompt"] = prompt
        return _verbose_json(
            " hello world",
            [(0.0, 1.0, " hello"), (1.0, 2.0, " world")],
            language="english",
        )

    monkeypatch.setattr(WhisperCppBackend, "_inference", fake_infer)
    out = be.transcribe_audio(media, model="m", language="en", prompt="EN.")
    assert captured["language"] == "en"
    assert captured["prompt"] == "EN."
    assert out.language == "en"
    assert out.text == "hello world"
    assert [s["start"] for s in out.segments] == [0.0, 1.0]


def test_region_path_offsets_and_merges(tmp_path: Path, monkeypatch):
    media = tmp_path / "long.mp4"
    media.write_bytes(b"\x00")
    be = WhisperCppBackend(endpoint="http://n5:8090")

    # Two speech regions far apart → sliced, transcribed, offset back.
    monkeypatch.setattr(
        plan_mod, "build_speech_plan",
        lambda m, **k: SpeechPlan(
            media=m, duration_s=600.0,
            regions=(SpeechRegion(10.0, 12.0), SpeechRegion(500.0, 502.0)),
            lang_candidates=(), initial_prompt=None),
    )
    monkeypatch.setattr(
        plan_mod, "materialize_region_wavs",
        lambda plan, work, **k: [
            (10.0, work / "r0.wav"), (500.0, work / "r1.wav")],
    )

    def fake_infer(self, wav, *, language, prompt):
        # region-local stamps (0-based per slice)
        return _verbose_json(" hi", [(0.0, 1.5, " hi")], language="russian")

    monkeypatch.setattr(WhisperCppBackend, "_inference", fake_infer)
    out = be.transcribe_audio(media, model="m", language=None, prompt=None)
    # offsets applied: 10.0 and 500.0, sorted
    assert [s["start"] for s in out.segments] == [10.0, 500.0]
    assert [s["end"] for s in out.segments] == [11.5, 501.5]
    # language=None → taken from server response ("russian" → "ru")
    assert out.language == "ru"


def test_detect_language_clamps_to_candidates(tmp_path: Path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00")
    be = WhisperCppBackend(endpoint="http://n5:8090")
    monkeypatch.setattr(
        plan_mod, "materialize_wav",
        lambda m, dst, **k: (dst.write_bytes(b"w"), dst)[1],
    )
    # Server says "welsh" (off-candidate noise) → clamp to first candidate.
    monkeypatch.setattr(
        WhisperCppBackend, "_inference",
        lambda self, wav, *, language, prompt: _verbose_json(
            "", [], language="welsh"),
    )
    got = be.detect_language(media, candidates=("en", "ru", "uk"), model="m")
    assert got == "en"

    # In-candidate detection is kept as-is.
    monkeypatch.setattr(
        WhisperCppBackend, "_inference",
        lambda self, wav, *, language, prompt: _verbose_json(
            "", [], language="russian"),
    )
    got = be.detect_language(media, candidates=("en", "ru", "uk"), model="m")
    assert got == "ru"


def test_detect_language_returns_none_on_failure(tmp_path: Path, monkeypatch):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"\x00")
    be = WhisperCppBackend(endpoint="http://n5:8090")
    monkeypatch.setattr(
        plan_mod, "materialize_wav",
        lambda m, dst, **k: (dst.write_bytes(b"w"), dst)[1],
    )

    def boom(self, wav, *, language, prompt):
        raise WhisperCppError("server down")

    monkeypatch.setattr(WhisperCppBackend, "_inference", boom)
    assert be.detect_language(
        media, candidates=("en", "ru"), model="m") is None
