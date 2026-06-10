"""Unit tests for `immy/transcripts.py` — format + excerpt + sidecar
path logic. The mlx-whisper call itself isn't exercised here (it needs
real audio + Apple Silicon); `_process_transcript` is covered via a
dummy `transcribe` in `test_process.py`.
"""

from __future__ import annotations

from pathlib import Path

from immy.transcripts import (
    excerpt_text,
    format_srt,
    is_denylisted_make,
    sidecar_path,
)


def test_format_srt_basic():
    segments = [
        {"start": 0.0, "end": 1.5, "text": "Hello world"},
        {"start": 1.5, "end": 3.25, "text": "  second line  "},
    ]
    out = format_srt(segments)
    assert "1\n00:00:00,000 --> 00:00:01,500\nHello world" in out
    assert "2\n00:00:01,500 --> 00:00:03,250\nsecond line" in out


def test_format_srt_skips_blank_segments():
    segments = [
        {"start": 0.0, "end": 1.0, "text": ""},
        {"start": 1.0, "end": 2.0, "text": "only one"},
    ]
    out = format_srt(segments)
    # Index re-numbers after dropping the blank — no "2" entry.
    assert out.startswith("1\n00:00:01,000 --> 00:00:02,000\nonly one")
    assert "2\n" not in out


def test_format_srt_millisecond_carry():
    # 0.9995 s → 1.000 s; millis must roll over cleanly, not print ",1000".
    segments = [{"start": 0.0, "end": 0.9995, "text": "x"}]
    out = format_srt(segments)
    assert "00:00:01,000" in out
    assert ",1000" not in out


def test_format_srt_collapses_repetition_loops():
    # Whisper decode loop: same cue stuck on repeat. Only the first
    # occurrence survives; surrounding real speech is untouched.
    segments = [{"start": 0.0, "end": 1.0, "text": "перед циклом"}]
    segments += [
        {"start": 1.0 + i, "end": 2.0 + i, "text": "Добро пожаловать в Казахстан!"}
        for i in range(10)
    ]
    segments += [{"start": 12.0, "end": 13.0, "text": "после цикла"}]
    out = format_srt(segments)
    assert out.count("Добро пожаловать в Казахстан!") == 1
    assert "перед циклом" in out
    assert "после цикла" in out


def test_format_srt_keeps_short_repeats():
    # 5 consecutive identical cues is below LOOP_MIN_RUN — could be a
    # real chant/countdown, must survive intact.
    segments = [
        {"start": float(i), "end": float(i + 1), "text": "давай давай"}
        for i in range(5)
    ]
    out = format_srt(segments)
    assert out.count("давай давай") == 5


def test_repetition_loop_indexes_separate_runs():
    from immy.hallucinations import repetition_loop_indexes

    # Two separate runs each collapse independently; the interleaved
    # line breaks the run. Normalisation ignores case/punctuation.
    texts = ["a!", "A", "a", "a.", "other", "b", "b", "b", "B!"]
    assert repetition_loop_indexes(texts, min_run=3) == {1, 2, 3, 6, 7, 8}


def test_hallucination_substring_forms():
    # User-confirmed: these are hallucinations in ANY form, including
    # embedded mid-sentence — not just as whole lines.
    from immy.hallucinations import is_hallucination

    assert is_hallucination("Продолжение следует...")
    assert is_hallucination("ну что ж, продолжение следует, друзья")
    assert is_hallucination("Субтитры делал DimaTorzok")
    assert is_hallucination("DimaTorzok")
    assert is_hallucination("dimatorzok")  # matching is case-insensitive
    assert is_hallucination("СУБТИТРЫ ДЕЛАЛ DIMATORZOK")
    assert is_hallucination("Дима Торжок")  # cyrillic surname alone
    assert is_hallucination("Субтитры подготовлены каналом XYZ")
    assert not is_hallucination("мы продолжаем следовать на север")


def test_repetition_loop_indexes_ignores_blank_runs():
    from immy.hallucinations import repetition_loop_indexes

    assert repetition_loop_indexes(["", "", "", "", ""]) == set()


def test_speech_intervals_inverts_silences(monkeypatch, tmp_path):
    # Pins the VideoInfo attribute contract (`duration_s` — an upstream
    # rename to `duration_seconds` once silently killed every transcript
    # via on_transcript_error="skip") and the silence-inversion logic,
    # without ffmpeg: probe + silencedetect output are both faked.
    import subprocess
    from types import SimpleNamespace

    from immy import transcripts as t

    stderr = (
        "[silencedetect @ 0x0] silence_start: 2.0\n"
        "[silencedetect @ 0x0] silence_end: 8.0 | silence_duration: 6.0\n"
    )
    monkeypatch.setattr(t.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        t.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=stderr),
    )
    monkeypatch.setattr(
        t.video_mod, "probe",
        lambda _: SimpleNamespace(duration_s=10.0),
    )
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"")
    result = t.speech_intervals(media)
    assert result == (10.0, [(0.0, 2.0), (8.0, 10.0)])


def test_excerpt_text_short_passthrough():
    assert excerpt_text("hello") == "hello"


def test_excerpt_text_collapses_whitespace():
    assert excerpt_text("  a   b\n\tc  ") == "a b c"


def test_excerpt_text_truncates_on_word_boundary():
    words = " ".join(["word"] * 200)  # each "word" = 4 chars + space
    out = excerpt_text(words, max_chars=50)
    assert out.endswith("…")
    assert len(out) <= 52  # 50 body + "…" + potential shave
    # Ends at a word boundary, never mid-"word".
    assert "word…" in out or "words" not in out


def test_sidecar_path_uses_compound_suffix(tmp_path: Path):
    media = tmp_path / "clip.mov"
    # `foo.mov.srt` would collide with DJI telemetry detection; `.en.srt`
    # keeps us clear because `Path.with_suffix(".srt")` → `foo.srt`.
    assert sidecar_path(media, "en") == tmp_path / "clip.en.srt"
    assert sidecar_path(media, "es") == tmp_path / "clip.es.srt"


def test_denylist_matches_drone_makes():
    # DJI drone clips have no audio streams, so skip the ffprobe round-trip.
    # Insta360 X-series clips can carry useful audio and should fall through
    # to the generic has-audio gate.
    assert is_denylisted_make("DJI")
    assert is_denylisted_make("dji")
    assert not is_denylisted_make("Insta360")
    assert not is_denylisted_make("Arashi Vision")  # Insta360's legal name
    assert not is_denylisted_make("Apple")
    assert not is_denylisted_make("GoPro")
    assert not is_denylisted_make(None)
    assert not is_denylisted_make("")


def test_sidecar_path_does_not_collide_with_dji_sibling(tmp_path: Path):
    from immy.srt import find_sibling

    media = tmp_path / "DJI_0001.MP4"
    tx = sidecar_path(media, "en")
    tx.write_text("dummy", encoding="utf-8")
    # DJI telemetry parser looks for `<stem>.srt` / `<stem>.SRT`; our
    # `.en.srt` must not be picked up as telemetry.
    assert find_sibling(media) is None
