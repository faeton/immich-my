"""Speech-region planning + slicing (Phase 1 shared layer).

`build_speech_plan` decides where speech is (reusing `transcripts.speech_intervals`)
and whether the clip qualifies for region-skipping. `materialize_region_wavs` and
`merge_segments` are the portable equivalent of mlx's encoder-level
`clip_timestamps` skip, for backends that can only transcribe a whole file at a
time (whisper.cpp, Qwen3-ASR): slice each speech region to a temp WAV, transcribe
it, then offset the per-region stamps back onto the original timeline.

The mlx backend does NOT use the slicing helpers — it passes `clip_timestamps`
natively, preserving today's Mac behavior exactly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .types import SpeechPlan, SpeechRegion


def build_speech_plan(
    media: Path,
    *,
    lang_candidates: tuple[str, ...],
    initial_prompt: str | None = None,
) -> SpeechPlan | None:
    """Plan speech regions for `media`, or None when ffmpeg/probe failed.

    Mirrors the qualifying logic that lived inline in `transcripts.transcribe`:
    only long clips whose speech is a small fraction of the runtime get split
    into regions; everything else returns an empty-region plan (transcribe the
    whole file). Pads regions so the decoder gets lead-in/tail-out context.
    """
    from .. import transcripts as t  # lazy: avoid import cycle at module load

    intervals_info = t.speech_intervals(media, pad_s=t.REGION_PAD_SECONDS)
    if intervals_info is None:
        return None
    total, intervals = intervals_info
    speech_total = sum(e - s for s, e in intervals)
    regions: tuple[SpeechRegion, ...] = ()
    if (
        total >= t.REGION_MIN_DURATION_SECONDS
        and speech_total > 0
        and speech_total / total < t.REGION_MAX_SPEECH_FRACTION
        and len(intervals) >= 1
    ):
        regions = tuple(SpeechRegion(start_s=s, end_s=e) for s, e in intervals)
    return SpeechPlan(
        media=media,
        duration_s=total,
        regions=regions,
        lang_candidates=lang_candidates,
        initial_prompt=initial_prompt,
    )


def materialize_region_wavs(
    plan: SpeechPlan,
    work_dir: Path,
    *,
    sample_rate: int = 16_000,
) -> list[tuple[float, Path]]:
    """Export each speech region to a mono 16k s16le WAV under `work_dir`.

    Returns `(offset_s, wav_path)` pairs — `offset_s` is the region's start on
    the original timeline, to be added back to each segment by `merge_segments`.
    Used only by non-mlx backends. Raises if ffmpeg is unavailable.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found; required to slice speech regions")
    work_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[float, Path]] = []
    for i, region in enumerate(plan.regions):
        wav = work_dir / f"region_{i:04d}.wav"
        args = [
            "ffmpeg", "-nostdin", "-hide_banner", "-y",
            "-ss", f"{region.start_s:.3f}",
            "-to", f"{region.end_s:.3f}",
            "-i", str(plan.media),
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(wav),
        ]
        subprocess.run(args, capture_output=True, check=True)
        out.append((region.start_s, wav))
    return out


def materialize_wav(
    media: Path,
    dst: Path,
    *,
    start_s: float | None = None,
    dur_s: float | None = None,
    sample_rate: int = 16_000,
) -> Path:
    """Decode `media` to a mono 16k s16le WAV at `dst` (the format whisper.cpp
    wants). Optional `start_s`/`dur_s` extract just a window — used for the
    whole-file path (no window) and the language-probe path (first ~30 s).
    Raises if ffmpeg is missing or the decode fails.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found; required to decode audio for ASR")
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-y"]
    if start_s is not None:
        args += ["-ss", f"{start_s:.3f}"]
    if dur_s is not None:
        args += ["-t", f"{dur_s:.3f}"]
    args += [
        "-i", str(media),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(dst),
    ]
    subprocess.run(args, capture_output=True, check=True)
    return dst


def clamp_language(detected: str | None, candidates: tuple[str, ...]) -> str | None:
    """Constrain a free-form detected language to the configured candidate set.

    whisper.cpp / Qwen will happily report `welsh` or `nynorsk` for noisy or
    music-only audio; on a known-multilingual library that's always wrong.
    Returns `detected` when it's already a candidate, else the first candidate
    (the safe default). None in → None out (caller falls back to auto).
    """
    if detected is None:
        return None
    if not candidates:
        return detected
    return detected if detected in candidates else candidates[0]


def merge_segments(
    per_region: list[tuple[float, list[dict]]],
) -> list[dict]:
    """Offset each region's segments by its start time and flatten in order.

    `per_region` is `[(offset_s, [{start,end,text}, ...]), ...]`. Region-local
    stamps (0-based per slice) become original-timeline stamps; the result is
    sorted by start so `format_srt` numbers cues correctly.
    """
    merged: list[dict] = []
    for offset_s, segments in per_region:
        for seg in segments:
            merged.append({
                "start": float(seg.get("start", 0.0)) + offset_s,
                "end": float(seg.get("end", 0.0)) + offset_s,
                "text": seg.get("text", ""),
            })
    merged.sort(key=lambda s: s["start"])
    return merged


__all__ = [
    "build_speech_plan", "materialize_region_wavs", "materialize_wav",
    "clamp_language", "merge_segments",
]
