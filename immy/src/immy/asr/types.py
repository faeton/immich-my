"""Shared ASR data types (Phase 1).

Pure dataclasses — imports nothing from the rest of `immy`, so any module
(including `transcripts.py` at top level) can pull these in without risking a
circular import or dragging in the Apple-only `mlx` backend.

`BackendTranscript` is the *raw* output an `AsrBackend` returns: untouched
segments straight from the model. `TranscriptResult` is the *finished* product
after the shared runner has rendered + scrubbed the SRT and written the sidecar.
`HallucinationOnly` / `HALLUCINATION_ONLY` keep their original identity here so
`isinstance(result, transcripts.HallucinationOnly)` in `process.py` still works
(transcripts.py re-exports the same objects).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackendTranscript:
    """Raw inference output from an `AsrBackend`.

    `segments` is a list of `{"start": float, "end": float, "text": str}`
    dicts on the *original media timeline* — the same shape mlx-whisper emits
    and the shape `transcripts.format_srt` consumes, so the mlx path needs no
    mapping. Non-mlx backends that transcribe sliced region WAVs must offset
    their per-region stamps back to the full timeline (see `plan.merge_segments`)
    before returning.
    """
    segments: list[dict]
    text: str
    language: str | None


@dataclass(frozen=True)
class TranscriptResult:
    srt_path: Path
    language: str
    excerpt: str


@dataclass(frozen=True)
class SpeechRegion:
    start_s: float
    end_s: float


@dataclass(frozen=True)
class SpeechPlan:
    """Where speech lives in a clip, plus the language/prompt hints to decode it.

    `regions` empty → transcribe the whole file (short clip, or mostly-speech).
    Non-empty → only these padded windows carry speech; backends may skip the
    silent stretches between them (mlx via `clip_timestamps`, others via
    per-region WAV slicing).
    """
    media: Path
    duration_s: float
    regions: tuple[SpeechRegion, ...]
    lang_candidates: tuple[str, ...]
    initial_prompt: str | None


class HallucinationOnly:
    """Marker — the runner returns `HALLUCINATION_ONLY` when a backend produced
    text but every segment was filtered as a known hallucination (so the caller
    can journal a meaningful skip reason instead of a fake-positive transcript).
    """
    __slots__ = ()


HALLUCINATION_ONLY = HallucinationOnly()


__all__ = [
    "BackendTranscript",
    "TranscriptResult",
    "SpeechRegion",
    "SpeechPlan",
    "HallucinationOnly",
    "HALLUCINATION_ONLY",
]
