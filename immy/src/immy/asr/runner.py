"""Backend-agnostic transcription orchestration (Phase 1).

One code path for every backend: detect language → run inference → render +
scrub SRT → write sidecar → return excerpt. The render/scrub/sidecar half is
exactly the logic that used to live at the tail of `transcripts.transcribe`, so
the mlx path produces identical output; only the inference call is now pluggable.
"""

from __future__ import annotations

from pathlib import Path

from .base import AsrBackend
from .types import HALLUCINATION_ONLY, HallucinationOnly, TranscriptResult


def transcribe_media(
    media: Path,
    backend: AsrBackend,
    *,
    model: str,
    language: str | None = None,
    lang_candidates: tuple[str, ...] | None = None,
    prompt: str | None = None,
) -> TranscriptResult | HallucinationOnly | None:
    """Transcribe one video via `backend`; write the .srt; return the excerpt.

    Returns None when the backend produced no text (silent clip / undetectable
    language), and `HALLUCINATION_ONLY` when it produced text but every segment
    was filtered as boilerplate. Callers are expected to have pre-checked
    `has_audio` / silence gates (process.py does).
    """
    from .. import transcripts as t

    if language is None and lang_candidates:
        language = backend.detect_language(
            media, candidates=lang_candidates, model=model,
        )
    raw = backend.transcribe_audio(
        media, model=model, language=language, prompt=prompt,
    )
    if not raw.text:
        return None
    detected_lang = str(raw.language or language or t.DEFAULT_LANG_CODE)
    # format_srt drops hallucinated/blank cues and collapses decode loops; if
    # nothing survives, the whole clip was boilerplate — signal that distinctly
    # from "no text at all" so the caller journals a meaningful skip reason.
    srt_body = t.format_srt(raw.segments)
    if not srt_body.strip():
        return HALLUCINATION_ONLY
    clean_text = t.srt_to_plaintext(srt_body)
    if not clean_text:
        return HALLUCINATION_ONLY
    dst = t.sidecar_path(media, detected_lang)
    dst.write_text(srt_body, encoding="utf-8")
    return TranscriptResult(
        srt_path=dst,
        language=detected_lang,
        excerpt=t.excerpt_text(clean_text),
    )


__all__ = ["transcribe_media"]
