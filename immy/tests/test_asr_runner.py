"""Tests for the Phase 1 ASR backend layer (`immy/asr/`).

Covers the shared runner orchestration, plan/merge helpers, the registry, and
the `transcripts` compat seam — all WITHOUT mlx (a fake backend stands in), so
these run anywhere, including Linux CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from immy.asr import plan, registry, runner
from immy.asr.types import (
    HALLUCINATION_ONLY,
    BackendTranscript,
    HallucinationOnly,
    SpeechPlan,
    SpeechRegion,
    TranscriptResult,
)


class FakeBackend:
    """Minimal AsrBackend: returns canned segments, records what it was asked."""

    name = "fake"

    def __init__(self, segments, text, language="en", detect="ru"):
        self._segments = segments
        self._text = text
        self._language = language
        self._detect = detect
        self.seen_language = "UNSET"
        self.detect_called_with = None

    def detect_language(self, media, *, candidates, model):
        self.detect_called_with = (candidates, model)
        return self._detect

    def transcribe_audio(self, media, *, model, language, prompt):
        self.seen_language = language
        # Echo the language the runner resolved so we can assert propagation;
        # falls back to the canned one when the runner passed None.
        return BackendTranscript(
            segments=self._segments,
            text=self._text,
            language=language or self._language,
        )


def _media(tmp_path: Path) -> Path:
    m = tmp_path / "clip.mov"
    m.write_bytes(b"")
    return m


def test_runner_happy_path_writes_sidecar(tmp_path):
    media = _media(tmp_path)
    be = FakeBackend(
        segments=[{"start": 0.0, "end": 1.5, "text": "Hello world"}],
        text="Hello world",
        language="en",
    )
    result = runner.transcribe_media(
        media, be, model="m", language="en", lang_candidates=None,
    )
    assert isinstance(result, TranscriptResult)
    assert result.language == "en"
    assert result.excerpt == "Hello world"
    assert result.srt_path == tmp_path / "clip.en.srt"
    assert result.srt_path.read_text(encoding="utf-8").startswith(
        "1\n00:00:00,000 --> 00:00:01,500\nHello world"
    )


def test_runner_none_on_empty_text(tmp_path):
    media = _media(tmp_path)
    be = FakeBackend(segments=[], text="", language="en")
    assert runner.transcribe_media(
        media, be, model="m", language="en") is None


def test_runner_hallucination_only_when_all_boilerplate(tmp_path):
    media = _media(tmp_path)
    # Non-empty text, but every cue is a known hallucination → no SRT survives.
    be = FakeBackend(
        segments=[{"start": 0.0, "end": 1.0, "text": "Субтитры делал DimaTorzok"}],
        text="Субтитры делал DimaTorzok",
        language="ru",
    )
    result = runner.transcribe_media(media, be, model="m", language="ru")
    assert result is HALLUCINATION_ONLY
    assert isinstance(result, HallucinationOnly)
    assert not (tmp_path / "clip.ru.srt").exists()


def test_runner_detects_language_when_unset(tmp_path):
    media = _media(tmp_path)
    be = FakeBackend(
        segments=[{"start": 0.0, "end": 1.0, "text": "привет"}],
        text="привет",
        detect="ru",
    )
    result = runner.transcribe_media(
        media, be, model="m", language=None, lang_candidates=("en", "ru", "uk"),
    )
    # detect_language was consulted and its answer propagated into inference + sidecar.
    assert be.detect_called_with == (("en", "ru", "uk"), "m")
    assert be.seen_language == "ru"
    assert isinstance(result, TranscriptResult)
    assert result.srt_path == tmp_path / "clip.ru.srt"


def test_merge_segments_offsets_and_sorts():
    per_region = [
        (10.0, [{"start": 0.0, "end": 1.0, "text": "b"}]),
        (0.0, [{"start": 0.5, "end": 1.0, "text": "a"}]),
    ]
    merged = plan.merge_segments(per_region)
    assert [s["text"] for s in merged] == ["a", "b"]  # sorted by absolute start
    assert merged[0]["start"] == 0.5
    assert merged[1]["start"] == 10.0 and merged[1]["end"] == 11.0


def test_build_speech_plan_regions_for_long_sparse(monkeypatch, tmp_path):
    from immy import transcripts as t

    media = _media(tmp_path)
    # 600 s clip, 30 s of speech in two windows → well under the speech-fraction
    # cap and over the duration floor → regions populated.
    monkeypatch.setattr(
        t, "speech_intervals",
        lambda m, **k: (600.0, [(10.0, 25.0), (100.0, 115.0)]),
    )
    sp = plan.build_speech_plan(media, lang_candidates=("en",))
    assert isinstance(sp, SpeechPlan)
    assert sp.duration_s == 600.0
    assert sp.regions == (
        SpeechRegion(10.0, 25.0), SpeechRegion(100.0, 115.0),
    )


def test_build_speech_plan_no_regions_for_short_clip(monkeypatch, tmp_path):
    from immy import transcripts as t

    media = _media(tmp_path)
    # Short clip → below REGION_MIN_DURATION_SECONDS → whole-file (empty regions).
    monkeypatch.setattr(
        t, "speech_intervals", lambda m, **k: (30.0, [(0.0, 20.0)]),
    )
    sp = plan.build_speech_plan(media, lang_candidates=("en",))
    assert sp is not None and sp.regions == ()


def test_build_speech_plan_none_when_probe_fails(monkeypatch, tmp_path):
    from immy import transcripts as t

    media = _media(tmp_path)
    monkeypatch.setattr(t, "speech_intervals", lambda m, **k: None)
    assert plan.build_speech_plan(media, lang_candidates=("en",)) is None


def test_registry_mlx_resolves():
    be = registry.get_backend("mlx")
    assert be.name == "mlx"


def test_registry_unimplemented_backends_raise():
    with pytest.raises(NotImplementedError):
        registry.get_backend("whispercpp")
    with pytest.raises(NotImplementedError):
        registry.get_backend("qwen-asr")


def test_registry_unknown_backend_raises():
    with pytest.raises(ValueError):
        registry.get_backend("nope")


def test_transcripts_reexports_are_same_objects():
    # process.py does isinstance(result, transcripts_mod.HallucinationOnly);
    # the re-exported names must be the SAME objects the runner returns.
    from immy import transcripts as t

    assert t.HallucinationOnly is HallucinationOnly
    assert t.HALLUCINATION_ONLY is HALLUCINATION_ONLY
    assert t.TranscriptResult is TranscriptResult


def test_transcripts_transcribe_rejects_unknown_backend(tmp_path):
    from immy import transcripts as t

    media = _media(tmp_path)
    with pytest.raises(ValueError):
        t.transcribe(media, backend="nope")
