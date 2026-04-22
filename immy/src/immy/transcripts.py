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


def has_audio(media: Path) -> bool:
    """True when ffprobe reports an audio stream. Whisper on silent
    footage wastes minutes and yields garbage, so skip early."""
    try:
        info = video_mod.probe(media)
    except video_mod.VideoProbeError:
        return False
    return bool(info.audio_codec)


def transcribe(
    media: Path,
    *,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
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
    kwargs: dict = {"path_or_hf_repo": model}
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["initial_prompt"] = prompt
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
    "transcribe",
]
