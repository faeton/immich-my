"""The `AsrBackend` protocol (Phase 1).

A backend is just two operations: pick a language, and turn audio into raw
segments. Everything else — silence gating, region planning, SRT rendering,
hallucination scrubbing, sidecar writing — lives in the shared layer
(`transcripts.py` + `asr/plan.py` + `asr/runner.py`) and is identical across
backends. Adding whisper.cpp or Qwen3-ASR (Phase 2/5) means implementing this
protocol, nothing more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .types import BackendTranscript


@runtime_checkable
class AsrBackend(Protocol):
    name: str  # "mlx" | "whispercpp" | "qwen-asr"

    def detect_language(
        self,
        media: Path,
        *,
        candidates: tuple[str, ...],
        model: str,
        seek_s: float | None = None,
    ) -> str | None:
        """Best language *from `candidates`*, or None when the backend can't
        decide (caller then falls back to a configured default / initial_prompt).
        `seek_s` overrides where in the clip the detection window starts.
        """
        ...

    def transcribe_audio(
        self,
        media: Path,
        *,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> BackendTranscript:
        """Transcribe `media`; return raw segments on the original timeline.

        The backend owns how it skips silence internally — mlx passes
        `clip_timestamps`, others slice region WAVs via `asr.plan` helpers.
        """
        ...


__all__ = ["AsrBackend"]
