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


# Whisper's other failure mode besides boilerplate: it gets stuck in a
# decode loop and emits the same cue over and over for the rest of the
# clip ("Добро пожаловать в Казахстан!" × 40 on wind noise). Real loops
# run 10-40 repeats; 6 keeps clear of genuine chants/call-and-response,
# which rarely produce even a handful of identical whole cues.
LOOP_MIN_RUN = 6


def repetition_loop_indexes(texts: list[str], min_run: int = LOOP_MIN_RUN) -> set[int]:
    """Indexes of cues that are loop repeats: members of a run of
    `min_run`+ consecutive identical (normalised) texts, except the
    run's first cue — that one occurrence may genuinely have been said,
    so it survives. Empty/whitespace texts never form runs.

    Used by `transcripts.format_srt` at write time and by
    `tools/scrub-srt-hallucinations.py` for previously-written SRTs.
    """
    drop: set[int] = set()
    run_start = 0
    run_norm: str | None = None
    run_len = 0

    def flush() -> None:
        if run_norm is not None and run_len >= min_run:
            drop.update(range(run_start + 1, run_start + run_len))

    for i, text in enumerate(texts):
        norm = _normalise(text)
        if norm and norm == run_norm:
            run_len += 1
            continue
        flush()
        run_start, run_norm, run_len = i, (norm or None), 1
    flush()
    return drop


__all__ = [
    "HALLUCINATION_PATTERNS",
    "LOOP_MIN_RUN",
    "is_hallucination",
    "repetition_loop_indexes",
]
