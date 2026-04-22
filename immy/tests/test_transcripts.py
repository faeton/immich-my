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
    # Every drone file in ~/Media/Trips produced by DJI/Insta360 has
    # zero audio streams. The denylist skips the ffprobe round-trip
    # entirely for these cameras.
    assert is_denylisted_make("DJI")
    assert is_denylisted_make("dji")
    assert is_denylisted_make("Insta360")
    assert is_denylisted_make("Arashi Vision")  # Insta360's legal name
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
