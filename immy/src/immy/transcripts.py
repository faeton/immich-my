"""Phase 3 — Whisper transcription for video assets.

Runs `mlx-whisper` locally on the Mac. For each source video:

- Write `<stem>.<lang>.srt` next to the original media. The compound
  suffix (`foo.mov` → `foo.en.srt`) keeps it clear of DJI-telemetry
  `.SRT` siblings that `srt.py` parses — `Path.with_suffix(".srt")`
  returns `foo.srt`, not `foo.en.srt`, so telemetry detection stays
  untouched. Regular `immy promote` rsyncs the .srt along with the
  original, so no separate derivative push path is needed.
- Return a plain-text excerpt (first ~500 chars) for callers to write
  into `asset_exif.description` so the trip is searchable in Immich.

The large-v3 model is pulled from Hugging Face on first use and cached
under `~/.cache/huggingface/hub/` — same cache path the rest of the
project already uses. No extra download when the model is already
present from another project.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import video as video_mod
from .asr.types import (  # re-exported: stable public surface for process.py + tests
    HALLUCINATION_ONLY,
    HallucinationOnly,
    TranscriptResult,
)
from .hallucinations import collapse_word_runs, is_hallucination, repetition_loop_indexes


DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"
DEFAULT_LANG_CODE = "en"
EXCERPT_MAX_CHARS = 500

# volumedetect runs at ~3–5× realtime; 5 s window is enough signal to
# distinguish "real audio" from "silent track or wind noise floor",
# without paying more than ~2 s wall-clock per clip.
SILENCE_SAMPLE_SECONDS = 5.0
SILENCE_SEEK_SECONDS = 2.0  # skip intro (some GoPros mute the first frames)
SILENCE_MAX_DB = -50.0  # mean_volume above this → probably real speech/ambient

# Languages the user actually speaks / records. Whisper's auto-detect
# is fed via a 30 s mel-spectrogram and on clips dominated by wind,
# music, or near-silence it routinely hallucinates low-resource
# languages (`fo`, `nn`, `cy`, `ja`, `haw`). Constraining detection to
# this set keeps `srt:<lang>` honest and avoids the model ever being
# decoded with a wrong tokenizer (which produces gibberish text and
# inflates runtime). Order matters only as a tiebreak / fallback.
DEFAULT_LANG_CANDIDATES: tuple[str, ...] = ("en", "ru", "uk")

# Full-file silencedetect: a long clip with <SPEECH_MIN_SECONDS of
# non-silent audio is treated as effectively silent and skipped, even
# if the 5 s sample window happened to land on a noisy patch. -30 dB
# / 2 s gap matches typical handheld speech vs ambient floors and is
# loose enough that whispered conversation still registers.
SPEECH_NOISE_DB = -30.0
SPEECH_MIN_GAP_SECONDS = 2.0
SPEECH_MIN_SECONDS = 5.0

# When the speech-only fraction of a long clip is sparse, hand Whisper a
# `clip_timestamps` list so it skips silent stretches at the encoder
# level — a 60-min video with 2 min of actual speech then costs ~2 min
# of inference instead of ~60 min. Below the duration threshold the
# overhead of region-splitting isn't worth it; above the speech-fraction
# threshold the file is mostly speech anyway and one sweep is fine.
REGION_MIN_DURATION_SECONDS = 120.0  # only chunk videos longer than this
REGION_MAX_SPEECH_FRACTION = 0.7  # only chunk when speech is < 70% of file
# Pad each speech region on both sides so words that start within the
# silence-gap padding still get full context. Whisper's decoder is also
# more accurate when each region has a beat of lead-in / tail-out audio.
REGION_PAD_SECONDS = 0.75

# Cameras whose output is reliably mute on every model we've seen.
# Short-circuits before any ffprobe call. Matched case-insensitively
# against exiftool's EXIF:Make / QuickTime:Make. DJI's Mavic / Mini /
# FPV / Avata lines ship without an audio stream at all, so the probe
# is wasted I/O. Insta360 used to be on this list but the X-series
# (X2/X3/X4) does record usable audio on 360-mounted clips; we drop
# Insta360/Arashi Vision back to the generic ffprobe `has_audio` gate.
TRANSCRIBE_MAKE_DENYLIST = ("dji",)


# Inference + the result/marker types now live under `immy/asr/`:
#   TranscriptResult, HallucinationOnly, HALLUCINATION_ONLY → asr.types
#   mlx-whisper call + constrained language ID            → asr.mlx_backend
#   render/scrub/sidecar orchestration                    → asr.runner
# This module keeps the portable, backend-neutral helpers (silence/speech
# detection, SRT rendering, hallucination scrub, sidecar naming) and exposes
# `transcribe` / `detect_language_constrained` as thin compat wrappers.


def _format_ts(seconds: float) -> str:
    """SRT cue timestamp: `HH:MM:SS,mmm`. Comma (not dot) for the
    millisecond separator is the spec — VLC and macOS Quick Look reject
    dot-separated stamps."""
    if seconds < 0:
        seconds = 0.0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    whole = int(s)
    ms = int(round((s - whole) * 1000))
    # 999.5 → 1000; carry into seconds so output stays lexically valid.
    if ms == 1000:
        whole += 1
        ms = 0
    return f"{int(h):02d}:{int(m):02d}:{whole:02d},{ms:03d}"


def format_srt(segments: list[dict]) -> str:
    """Render mlx-whisper segments (`{start, end, text}`) as SRT.

    Empty-text segments are dropped — Whisper occasionally emits blank
    cues during long silences and they bloat the sidecar for no benefit.
    Cues that match a known hallucination pattern (DimaTorzok credits,
    "Продолжение следует", YouTube-outro thanks, etc.) are also dropped
    so the sidecar reflects only what was actually said. Decode-loop
    repeats (the same cue stuck on repeat for the rest of the clip)
    collapse to their first occurrence, and so do word-level loops
    inside a single cue («селфи» ×55 packed into one segment).
    """
    texts = [collapse_word_runs(str(seg.get("text", "")).strip()) for seg in segments]
    # Detect decode loops on the non-empty cue stream, the way the cues
    # will actually sit in the written SRT. Whisper interleaves blank
    # segments through silence, and computing runs over the raw list
    # let blanks break a run of identical cues — "Wood Wood" ×7 around
    # silent gaps sailed through (2026-06, la-manga/blue-lagoon).
    nonempty = [i for i, t in enumerate(texts) if t]
    loop_drop = {
        nonempty[j]
        for j in repetition_loop_indexes([texts[i] for i in nonempty])
    }
    lines: list[str] = []
    index = 1
    for i, seg in enumerate(segments):
        text = texts[i]
        if not text:
            continue
        if is_hallucination(text):
            continue
        if i in loop_drop:
            continue
        start = _format_ts(float(seg.get("start", 0.0)))
        end = _format_ts(float(seg.get("end", 0.0)))
        lines.append(str(index))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
        index += 1
    return "\n".join(lines)


def srt_to_plaintext(srt: str) -> str:
    """Strip cue indices + timing lines from an SRT blob, leaving only
    spoken text. Used to rebuild the description excerpt from a cached
    sidecar when the DB side wasn't written on the original pass.
    """
    lines: list[str] = []
    for raw in srt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
        lines.append(line)
    return " ".join(lines)


def excerpt_text(full_text: str, *, max_chars: int = EXCERPT_MAX_CHARS) -> str:
    """Collapse whitespace and truncate at a word boundary for
    `asset_exif.description`. Adds an ellipsis when truncated so it's
    obvious the description is partial."""
    collapsed = " ".join(full_text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    cut = collapsed[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:—- ")
    return f"{cut}…"


def sidecar_path(media: Path, lang_code: str = DEFAULT_LANG_CODE) -> Path:
    """`foo.mov` → `foo.en.srt`. Compound suffix avoids collision with
    DJI telemetry `.SRT` siblings (`foo.srt` / `foo.SRT`), which
    `srt.find_sibling` looks for via `Path.with_suffix`."""
    return media.with_name(f"{media.stem}.{lang_code}.srt")


def is_denylisted_make(make: str | None) -> bool:
    """Skip Whisper for cameras that never produce meaningful audio.
    DJI + Insta360 families don't record audio at all in video mode,
    so there's no point probing — the EXIF make string alone is enough.
    """
    if not make:
        return False
    lowered = make.strip().lower()
    return any(lowered.startswith(p) for p in TRANSCRIBE_MAKE_DENYLIST)


_VOLUMEDETECT_MEAN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def is_silent(
    media: Path,
    *,
    sample_s: float = SILENCE_SAMPLE_SECONDS,
    seek_s: float = SILENCE_SEEK_SECONDS,
    threshold_db: float = SILENCE_MAX_DB,
) -> bool:
    """True when the audio track is effectively silent.

    Runs `ffmpeg -af volumedetect` on a short sample window (seek past
    the intro, sample a few seconds). The decode is fast — ~2 s real
    time for a 5 s window on Apple Silicon. If ffmpeg can't parse a
    `mean_volume:` line we err on the side of "not silent" so borderline
    files still get a Whisper pass rather than being silently dropped.
    """
    if shutil.which("ffmpeg") is None:
        return False
    args = [
        "ffmpeg", "-nostdin", "-hide_banner",
        "-ss", f"{max(seek_s, 0.0):.3f}",
        "-t", f"{max(sample_s, 0.5):.3f}",
        "-i", str(media),
        "-vn", "-af", "volumedetect",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return False
    # volumedetect writes to stderr (the "null" muxer is stdout).
    match = _VOLUMEDETECT_MEAN.search(proc.stderr or "")
    if not match:
        return False
    try:
        mean_db = float(match.group(1))
    except ValueError:
        return False
    return mean_db < threshold_db


_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def speech_intervals(
    media: Path,
    *,
    noise_db: float = SPEECH_NOISE_DB,
    min_gap_s: float = SPEECH_MIN_GAP_SECONDS,
    pad_s: float = 0.0,
) -> tuple[float, list[tuple[float, float]]] | None:
    """Returns `(total_duration, speech_intervals)` for a media file.

    Runs `ffmpeg -af silencedetect` (decode-only, ~tens of seconds for
    a 60-min clip) and inverts the silent regions to speech regions.
    Each interval is optionally padded by `pad_s` on both sides — useful
    when feeding the result to Whisper, where leading silence/context
    helps the decoder transcribe the first word of each region cleanly.
    Padded intervals that overlap are merged so we don't hand Whisper
    redundant work.

    Returns None when ffmpeg/probe fail, so callers can fall back to
    the cheaper `is_silent` window sample.
    """
    if shutil.which("ffmpeg") is None:
        return None
    args = [
        "ffmpeg", "-nostdin", "-hide_banner",
        "-i", str(media),
        "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_gap_s}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        info = video_mod.probe(media)
    except video_mod.VideoProbeError:
        return None
    total = float(info.duration_s or 0.0)
    if total <= 0:
        return None
    starts = [float(m.group(1)) for m in _SILENCE_START.finditer(proc.stderr or "")]
    ends = [float(m.group(1)) for m in _SILENCE_END.finditer(proc.stderr or "")]
    # Build silence intervals; trailing unmatched silence_start runs to EOF.
    silences: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        silences.append((max(0.0, s), min(e, total)))
    # Invert: speech regions are the gaps between silences.
    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in silences:
        if s > cursor:
            speech.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total:
        speech.append((cursor, total))
    # Pad + merge.
    if pad_s > 0.0 and speech:
        padded = [(max(0.0, s - pad_s), min(total, e + pad_s)) for s, e in speech]
        merged: list[tuple[float, float]] = [padded[0]]
        for s, e in padded[1:]:
            ps, pe = merged[-1]
            if s <= pe:
                merged[-1] = (ps, max(pe, e))
            else:
                merged.append((s, e))
        speech = merged
    return total, speech


def speech_seconds(
    media: Path,
    *,
    noise_db: float = SPEECH_NOISE_DB,
    min_gap_s: float = SPEECH_MIN_GAP_SECONDS,
) -> float | None:
    """Total non-silent audio duration. Thin wrapper over `speech_intervals`."""
    result = speech_intervals(media, noise_db=noise_db, min_gap_s=min_gap_s)
    if result is None:
        return None
    _, intervals = result
    return sum(e - s for s, e in intervals)


def has_audio(media: Path) -> bool:
    """True when ffprobe reports an audio stream. Whisper on silent
    footage wastes minutes and yields garbage, so skip early."""
    try:
        info = video_mod.probe(media)
    except video_mod.VideoProbeError:
        return False
    return bool(info.audio_codec)


def detect_language_constrained(
    media: Path,
    *,
    candidates: tuple[str, ...] = DEFAULT_LANG_CANDIDATES,
    model: str = DEFAULT_MODEL,
    seek_s: float = SILENCE_SEEK_SECONDS,
) -> str | None:
    """Compat wrapper — constrained language ID via the mlx backend.

    Kept under this name for callers/tests that import it; the implementation
    moved to `asr.mlx_backend.MlxWhisperBackend.detect_language`. `seek_s` is
    forwarded so the public signature stays honest.
    """
    from .asr.mlx_backend import MlxWhisperBackend

    return MlxWhisperBackend().detect_language(
        media, candidates=candidates, model=model, seek_s=seek_s,
    )


def transcribe(
    media: Path,
    *,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    lang_candidates: tuple[str, ...] | None = DEFAULT_LANG_CANDIDATES,
    prompt: str | None = None,
    backend: str = "mlx",
    endpoint: str | None = None,
    sidecar_path: "Callable[[Path, str], Path] | None" = None,
) -> TranscriptResult | HallucinationOnly | None:
    """Transcribe one video; write the .srt sidecar; return the excerpt.

    Thin wrapper over the pluggable ASR layer. `backend` selects the inference
    engine — "mlx" (default; unchanged Mac behavior) and "whispercpp" (Phase 2;
    HTTP to a whisper-server at `endpoint`) are wired; "qwen-asr" is Phase 5
    (see raw/IMMY-ON-N5.md). `endpoint` is the backend's HTTP URL when it speaks
    HTTP (whispercpp); ignored by the in-process mlx path. Language
    detection, SRT render/scrub, and sidecar writing are shared across backends
    in `asr.runner`.

    Returns None when the backend produced no text (silent clip / undetectable
    language) and HALLUCINATION_ONLY when every segment was boilerplate. Caller
    is expected to have pre-checked `has_audio(media)`.

    `prompt` is passed through as the decoder's `initial_prompt` — a short phrase
    in the expected language(s) biases tokenisation toward the right script.
    """
    from .asr import registry, runner

    be = registry.get_backend(backend, endpoint=endpoint)
    return runner.transcribe_media(
        media, be,
        model=model,
        language=language,
        lang_candidates=lang_candidates,
        prompt=prompt,
        sidecar_path=sidecar_path,
    )


__all__ = [
    "DEFAULT_MODEL", "DEFAULT_LANG_CODE", "EXCERPT_MAX_CHARS",
    "TranscriptResult", "HallucinationOnly", "HALLUCINATION_ONLY",
    "format_srt", "excerpt_text", "srt_to_plaintext",
    "sidecar_path", "has_audio", "is_silent", "is_denylisted_make",
    "speech_seconds", "speech_intervals", "detect_language_constrained",
    "transcribe", "DEFAULT_LANG_CANDIDATES", "SPEECH_MIN_SECONDS",
]
