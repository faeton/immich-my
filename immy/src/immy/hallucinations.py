"""Whisper hallucination denylist.

Whisper sometimes emits training-corpus boilerplate (fansub credits,
YouTube outros, applause/music tags) on silent or noisy audio, even
after VAD trimming and language constraints. Patterns here are matched
against a normalised cue body (lowercase, trimmed, collapsed whitespace,
trailing punctuation/quotes stripped). Whole-line semantics keep us
from nuking real speech that merely contains a phrase.

Used by:
- `transcripts.format_srt` to drop hallucinated segments at write time
- `transcripts.transcribe` to distinguish hallucination-only output
  from genuine empty/silent output
- `tools/scrub-srt-hallucinations.py` to clean previously-written SRTs
"""
from __future__ import annotations

import re

HALLUCINATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Russian / Ukrainian — the DimaTorzok credit is the most common
    # hallucination on Russian-language footage; the rest are well-known
    # fansub boilerplate Whisper picked up from training data.
    re.compile(r"^продолжение следует$"),
    re.compile(r"^(субтитры|добавил субтитры).*(dimatorzok|торжок)"),
    re.compile(r"^субтитры (сделал|создал|создавал|делал|подогнал|подготовил)\b.*"),
    re.compile(r"^редактор субтитров\b.*"),
    re.compile(r"^корректор\b.*"),
    re.compile(r"^спасибо за просмотр$"),
    re.compile(r"^спасибо за субтитры\b.*"),
    re.compile(r"^подпишись на канал$"),
    re.compile(r"^подписывайтесь на канал$"),
    re.compile(r"^аплодисменты$"),
    re.compile(r"^динамичная музыка$"),
    re.compile(r"^спокойная музыка$"),
    re.compile(r"^превед,? медвед$"),
    # English
    re.compile(r"^thanks? for watching$"),
    re.compile(r"^thank you for watching$"),
    re.compile(r"^subtitles by\b.*"),
    re.compile(r"^subtitles by the amara\.org community$"),
    re.compile(r"^transcribed by\b.*"),
    re.compile(r"^please subscribe\b.*"),
    re.compile(r"^like and subscribe\b.*"),
    re.compile(r"^i'?ll see you next time$"),
    re.compile(r"^applause$"),
    re.compile(r"^music$"),
    # Music-note-only cues (any unicode music symbols + spaces only).
    re.compile(r"^[♩-♯\s]+$"),
)


_NORM_TRAIL = " \t.,!?…\"'«»“”‘’()[]"


def _normalise(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed.strip(_NORM_TRAIL).lower()


def is_hallucination(line: str) -> bool:
    """True if `line` matches any known Whisper hallucination pattern.

    A multi-line cue should be considered hallucination only when EVERY
    line matches — callers handle that semantics; this fn is per-line.
    """
    norm = _normalise(line)
    if not norm:
        return False
    return any(p.search(norm) for p in HALLUCINATION_PATTERNS)


__all__ = ["HALLUCINATION_PATTERNS", "is_hallucination"]
