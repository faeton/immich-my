"""mlx-whisper backend (Phase 1).

The Apple-Silicon inference path, lifted verbatim from the old
`transcripts.transcribe` / `detect_language_constrained` so Mac behavior is
byte-for-byte unchanged. This is the ONLY module that imports `mlx_whisper`, so
`import immy.transcripts` (and the rest of the package) stays usable on Linux —
the import is lazy and only fails when this backend is actually invoked.

Keeps the native `clip_timestamps` skip: for long sparse-speech clips, mlx is
handed the speech-only timeline so inference cost scales with speech duration,
not file duration — no WAV slicing needed on this path.
"""

from __future__ import annotations

from pathlib import Path

from .types import BackendTranscript


try:
    import mlx_whisper  # type: ignore
except ImportError:
    mlx_whisper = None  # Apple Silicon only; guarded at call site


def _require_mlx_whisper() -> None:
    if mlx_whisper is None:
        raise RuntimeError(
            "mlx-whisper is unavailable; install it via `uv sync` on Apple "
            "Silicon (mlx-whisper is x86-unsupported upstream). On the NAS use "
            "whisper_backend: whispercpp instead."
        )


class MlxWhisperBackend:
    name = "mlx"

    def detect_language(
        self,
        media: Path,
        *,
        candidates: tuple[str, ...],
        model: str,
        seek_s: float | None = None,
    ) -> str | None:
        """Most-likely language *from `candidates`* via mlx-whisper's language
        head, renormalising the softmax over the candidate set only. Returns
        None if mlx-whisper or its internals aren't available — caller falls
        back to auto-detect / initial_prompt."""
        from .. import transcripts as t

        _require_mlx_whisper()
        try:
            from mlx_whisper.audio import (  # type: ignore
                SAMPLE_RATE, N_SAMPLES, load_audio, log_mel_spectrogram, pad_or_trim,
            )
            from mlx_whisper.decoding import detect_language as _detect  # type: ignore
            from mlx_whisper.load_models import load_model  # type: ignore
            from mlx_whisper.tokenizer import get_tokenizer  # type: ignore
        except ImportError:
            return None
        try:
            m = load_model(model)
            audio = load_audio(str(media))
            # 30 s window after the intro — matches the chunk size the language
            # head was trained on. pad_or_trim zero-pads short tails.
            seek = t.SILENCE_SEEK_SECONDS if seek_s is None else seek_s
            start = int(max(seek, 0.0) * SAMPLE_RATE)
            audio = audio[start : start + N_SAMPLES]
            mel = log_mel_spectrogram(
                pad_or_trim(audio, N_SAMPLES), n_mels=m.dims.n_mels,
            )
            tokenizer = get_tokenizer(m.is_multilingual, num_languages=m.num_languages)
            _, probs_list = _detect(m, mel, tokenizer)
        except Exception:
            return None
        # detect_language returns a bare dict for a single spectrogram (its
        # `single` path) but a list for batched mel; we always pass one window.
        if isinstance(probs_list, dict):
            probs = probs_list
        else:
            probs = probs_list[0] if probs_list else {}
        if not probs:
            return candidates[0] if candidates else None
        best = max(candidates, key=lambda c: float(probs.get(c, 0.0)))
        return best

    def transcribe_audio(
        self,
        media: Path,
        *,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> BackendTranscript:
        from .. import transcripts as t
        from .plan import build_speech_plan

        _require_mlx_whisper()
        # condition_on_previous_text=False breaks the hallucination feedback
        # loop (each chunk priming the next into runaway boilerplate).
        # word_timestamps + hallucination_silence_threshold let the decoder
        # skip silent gaps >2 s when a window looks hallucinated.
        kwargs: dict = {
            "path_or_hf_repo": model,
            "condition_on_previous_text": False,
            "word_timestamps": True,
            "hallucination_silence_threshold": 2.0,
        }
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["initial_prompt"] = prompt
        # Native skip: hand mlx the speech-only timeline via clip_timestamps so
        # inference cost scales with speech, not file duration. Stamps stay in
        # the original timeline (mlx's chunker handles the offset internally).
        plan = build_speech_plan(media, lang_candidates=())
        if plan is not None and plan.regions:
            kwargs["clip_timestamps"] = [
                round(v, 3)
                for r in plan.regions
                for v in (r.start_s, r.end_s)
            ]
        result = mlx_whisper.transcribe(str(media), **kwargs)
        return BackendTranscript(
            segments=result.get("segments") or [],
            text=str(result.get("text") or "").strip(),
            language=str(result.get("language") or language or ""),
        )


__all__ = ["MlxWhisperBackend", "_require_mlx_whisper"]
