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
from dataclasses import dataclass
from pathlib import Path

from . import video as video_mod


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

# Cameras whose output is known-silent or known-non-speech. Short-circuits
# before any ffprobe call. Matched case-insensitively against exiftool's
# EXIF:Make / QuickTime:Make. DJI + Insta360 ship videos with no audio
# stream at all on every model we've seen (Mavic, Mini5Pro, FPV, Avata,
# ONE X2/X3/X4); keeping them here saves the probe round-trip.
TRANSCRIBE_MAKE_DENYLIST = ("dji", "insta360", "arashi vision")


try:
    import mlx_whisper  # type: ignore
except ImportError:
    mlx_whisper = None  # mlx-whisper is Apple Silicon only; guard at call site


@dataclass(frozen=True)
class TranscriptResult:
    srt_path: Path
    language: str
    excerpt: str


def _require_mlx_whisper() -> None:
    if mlx_whisper is None:
        raise RuntimeError(
            "mlx-whisper is unavailable; install it via `uv sync` on Apple "
            "Silicon (mlx-whisper is x86-unsupported upstream)."
        )


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
    """
    lines: list[str] = []
    index = 1
    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if not text:
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
    total = float(info.duration_seconds or 0.0)
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
    """Pick the most-likely language *from the candidate set*.

    Whisper's auto-detect sees every supported language and on noisy /
    near-silent clips often locks onto a low-resource one (`fo`, `nn`,
    `ja`). This runs the same language head but renormalises the
    softmax over `candidates` only, so the answer is always one the
    user actually speaks. Returns None if mlx-whisper or its internals
    aren't available — caller should then fall back to auto-detect.
    """
    _require_mlx_whisper()
    try:
        from mlx_whisper.audio import (
            SAMPLE_RATE, N_SAMPLES, load_audio, log_mel_spectrogram, pad_or_trim,
        )
        from mlx_whisper.decoding import detect_language as _detect
        from mlx_whisper.load_models import load_model
        from mlx_whisper.tokenizer import get_tokenizer
    except ImportError:
        return None
    try:
        m = load_model(model)
        audio = load_audio(str(media))
        # 30 s window starting after the intro — matches the chunk size
        # the language head was trained on. Bare slice is fine; pad_or_trim
        # handles short tails by zero-padding.
        start = int(max(seek_s, 0.0) * SAMPLE_RATE)
        audio = audio[start : start + N_SAMPLES]
        mel = log_mel_spectrogram(pad_or_trim(audio, N_SAMPLES), n_mels=m.dims.n_mels)
        tokenizer = get_tokenizer(m.is_multilingual, num_languages=m.num_languages)
        _, probs_list = _detect(m, mel, tokenizer)
    except Exception:
        return None
    probs = probs_list[0] if probs_list else {}
    if not probs:
        return candidates[0] if candidates else None
    best = max(candidates, key=lambda c: float(probs.get(c, 0.0)))
    return best


def transcribe(
    media: Path,
    *,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    lang_candidates: tuple[str, ...] | None = DEFAULT_LANG_CANDIDATES,
    prompt: str | None = None,
) -> TranscriptResult | None:
    """Transcribe one video; write the .srt sidecar; return excerpt.

    Returns None when Whisper produced no text (silent clip, or a
    language Whisper couldn't latch onto). Caller is expected to have
    pre-checked `has_audio(media)` — we don't re-probe here to avoid
    the duplicate ffprobe call when the caller already did it.

    `prompt` is passed through as Whisper's `initial_prompt`. Best use
    is a short phrase in the language(s) you expect — it biases the
    tokenizer toward the right vocabulary and script for mixed-language
    corpora where auto-detect would otherwise land on a close neighbour
    (e.g. Russian vs Ukrainian, or German vs Dutch).
    """
    _require_mlx_whisper()
    if language is None and lang_candidates:
        language = detect_language_constrained(
            media, candidates=lang_candidates, model=model,
        )
    kwargs: dict = {"path_or_hf_repo": model}
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["initial_prompt"] = prompt
    # For long, sparse-speech files (e.g. a 60-min hike with 2 min of
    # actual chatter), hand Whisper the speech-only timeline via
    # `clip_timestamps`. Inference cost then scales with speech duration,
    # not file duration, and SRT cue stamps stay correct in the original
    # video timeline (Whisper's chunker handles the offset internally).
    intervals_info = speech_intervals(media, pad_s=REGION_PAD_SECONDS)
    if intervals_info is not None:
        total, intervals = intervals_info
        speech_total = sum(e - s for s, e in intervals)
        if (
            total >= REGION_MIN_DURATION_SECONDS
            and speech_total > 0
            and speech_total / total < REGION_MAX_SPEECH_FRACTION
            and len(intervals) >= 1
        ):
            # Flat [s0,e0,s1,e1,...] — mlx_whisper accepts list[float].
            kwargs["clip_timestamps"] = [
                round(v, 3) for s, e in intervals for v in (s, e)
            ]
    result = mlx_whisper.transcribe(str(media), **kwargs)
    segments = result.get("segments") or []
    full_text = str(result.get("text") or "").strip()
    if not full_text:
        return None
    detected_lang = str(result.get("language") or language or DEFAULT_LANG_CODE)
    dst = sidecar_path(media, detected_lang)
    dst.write_text(format_srt(segments), encoding="utf-8")
    return TranscriptResult(
        srt_path=dst,
        language=detected_lang,
        excerpt=excerpt_text(full_text),
    )


__all__ = [
    "DEFAULT_MODEL", "DEFAULT_LANG_CODE", "EXCERPT_MAX_CHARS",
    "TranscriptResult", "format_srt", "excerpt_text", "srt_to_plaintext",
    "sidecar_path", "has_audio", "is_silent", "is_denylisted_make",
    "speech_seconds", "speech_intervals", "detect_language_constrained",
    "transcribe", "DEFAULT_LANG_CANDIDATES", "SPEECH_MIN_SECONDS",
]
