"""whisper.cpp backend (Phase 2) — HTTP to a whisper-server `/inference`.

The NAS ASR path. immy stays a thin orchestrator: the heavy lifting (Vulkan
inference on the N5's 890M iGPU) runs in the `ghcr.io/ggml-org/whisper.cpp`
server container, reachable over HTTP — same shape as captions (Ollama) and
CLIP (immich-ml). No GPU, no docker socket, no model weights on the immy side.

whisper-server speaks an OpenAI-ish multipart endpoint:

    POST {endpoint}/inference
      file=<audio>            (multipart; we send mono 16k s16le WAV)
      response_format=verbose_json
      language=en|ru|...|auto
      prompt=<initial prompt>
    -> {"language": "english", "text": "...",
        "segments": [{"start": 0.0, "end": 6.7, "text": " ...", ...}, ...]}

The segment shape (`start`/`end`/`text`) is exactly what the shared
`format_srt` / `merge_segments` consume, so the render/scrub/sidecar half is
identical to every other backend. Long sparse-speech clips are sliced into
speech regions client-side (the portable equivalent of mlx's `clip_timestamps`
skip) and each region POSTed, then stamps offset back onto the original
timeline.

The server is started with a fixed `-m <model>`; immy's `model` arg is
informational here (the server owns its weights). Swap models at deploy, not
per request.
"""

from __future__ import annotations

import json
import mimetypes
import subprocess
import urllib.request
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from .types import BackendTranscript


# whisper.cpp reports languages by full English name in verbose_json
# ("english", "russian", ...). The canonical openai-whisper code↔name table,
# inverted to map name → ISO 639-1 code so sidecars/DB store "en"/"ru"/"uk".
# Unknown names pass through lowercased; `plan.clamp_language` then pushes them
# onto the candidate set, so a missing entry degrades gracefully.
_WHISPER_CODE_TO_NAME = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish",
    "ru": "russian", "ko": "korean", "fr": "french", "ja": "japanese",
    "pt": "portuguese", "tr": "turkish", "pl": "polish", "ca": "catalan",
    "nl": "dutch", "ar": "arabic", "sv": "swedish", "it": "italian",
    "id": "indonesian", "hi": "hindi", "fi": "finnish", "vi": "vietnamese",
    "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay",
    "cs": "czech", "ro": "romanian", "da": "danish", "hu": "hungarian",
    "ta": "tamil", "no": "norwegian", "th": "thai", "ur": "urdu",
    "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian", "la": "latin",
    "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak",
    "te": "telugu", "fa": "persian", "lv": "latvian", "bn": "bengali",
    "sr": "serbian", "az": "azerbaijani", "sl": "slovenian", "kn": "kannada",
    "et": "estonian", "mk": "macedonian", "br": "breton", "eu": "basque",
    "is": "icelandic", "hy": "armenian", "ne": "nepali", "mn": "mongolian",
    "bs": "bosnian", "kk": "kazakh", "sq": "albanian", "sw": "swahili",
    "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala",
    "km": "khmer", "sn": "shona", "yo": "yoruba", "so": "somali",
    "af": "afrikaans", "oc": "occitan", "ka": "georgian", "be": "belarusian",
    "tg": "tajik", "sd": "sindhi", "gu": "gujarati", "am": "amharic",
    "yi": "yiddish", "lo": "lao", "uz": "uzbek", "fo": "faroese",
    "ht": "haitian creole", "ps": "pashto", "tk": "turkmen", "nn": "nynorsk",
    "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar",
    "bo": "tibetan", "tl": "tagalog", "mg": "malagasy", "as": "assamese",
    "tt": "tatar", "haw": "hawaiian", "ln": "lingala", "ha": "hausa",
    "ba": "bashkir", "jw": "javanese", "su": "sundanese",
}
_WHISPER_NAME_TO_CODE = {v: k for k, v in _WHISPER_CODE_TO_NAME.items()}


def _lang_name_to_code(name: str | None) -> str | None:
    """Map whisper.cpp's full language name to an ISO 639-1 code. Already-coded
    values (len 2/3) pass through; unknowns return lowercased for the clamp."""
    if not name:
        return None
    low = name.strip().lower()
    if low in _WHISPER_NAME_TO_CODE:
        return _WHISPER_NAME_TO_CODE[low]
    if low in _WHISPER_CODE_TO_NAME:  # already a code
        return low
    return low


def _encode_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    """Build a multipart/form-data body (stdlib only — no requests dep)."""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    out: list[bytes] = []
    for key, value in fields.items():
        out += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{key}"'.encode(),
            b"",
            str(value).encode("utf-8"),
        ]
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    out += [
        f"--{boundary}".encode(),
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{file_path.name}"'
        ).encode(),
        f"Content-Type: {ctype}".encode(),
        b"",
        file_path.read_bytes(),
    ]
    out += [f"--{boundary}--".encode(), b""]
    body = crlf.join(out)
    return body, f"multipart/form-data; boundary={boundary}"


class WhisperCppError(RuntimeError):
    pass


class WhisperCppBackend:
    name = "whispercpp"

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_s: float = 1800.0,
        sample_rate: int = 16_000,
    ) -> None:
        if not endpoint:
            raise WhisperCppError(
                "whisper_backend 'whispercpp' needs ml.whisper_endpoint "
                "(the whisper-server URL, e.g. http://n5:8090)"
            )
        # Accept either the base URL ("http://n5:8090") or the full route
        # ("http://n5:8090/inference") — `_inference` appends "/inference", so
        # strip a trailing one to avoid "/inference/inference".
        ep = endpoint.rstrip("/")
        if ep.endswith("/inference"):
            ep = ep[: -len("/inference")]
        self.endpoint = ep
        self.timeout_s = timeout_s
        self.sample_rate = sample_rate

    # -- HTTP --------------------------------------------------------------

    def _inference(
        self,
        wav: Path,
        *,
        language: str | None,
        prompt: str | None,
    ) -> dict:
        fields = {"response_format": "verbose_json", "language": language or "auto"}
        if prompt:
            fields["prompt"] = prompt
        body, content_type = _encode_multipart(fields, wav)
        req = urllib.request.Request(
            f"{self.endpoint}/inference",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except Exception as e:  # urllib.error.URLError, socket.timeout, ...
            raise WhisperCppError(
                f"whisper-server request to {self.endpoint}/inference failed: {e}"
            ) from e
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise WhisperCppError(
                f"whisper-server returned non-JSON: {raw[:200]!r}"
            ) from e

    @staticmethod
    def _segments(payload: dict) -> list[dict]:
        """Normalise the server's segments to the {start,end,text} shape the
        shared render/merge layer expects (drops words/tokens/probs)."""
        out: list[dict] = []
        for seg in payload.get("segments") or []:
            out.append({
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": seg.get("text", ""),
            })
        return out

    # -- AsrBackend protocol ----------------------------------------------

    def detect_language(
        self,
        media: Path,
        *,
        candidates: tuple[str, ...],
        model: str,
        seek_s: float | None = None,
    ) -> str | None:
        """Probe the first ~30 s with language=auto, map the reported language
        to an ISO code, and clamp it to `candidates`. Returns None on any
        failure so the caller falls back to auto-detect during the full pass.
        """
        from . import plan as plan_mod

        start = 0.0 if seek_s is None else max(seek_s, 0.0)
        try:
            with TemporaryDirectory(prefix="immy-langprobe-") as td:
                wav = plan_mod.materialize_wav(
                    media, Path(td) / "probe.wav",
                    start_s=start, dur_s=30.0, sample_rate=self.sample_rate,
                )
                payload = self._inference(wav, language="auto", prompt=None)
        except (WhisperCppError, RuntimeError, OSError, subprocess.SubprocessError):
            # ffmpeg probe (materialize_wav, check=True) raises
            # CalledProcessError — a probe failure must fall back to full-pass
            # auto-detect, never abort ASR.
            return None
        detected = _lang_name_to_code(payload.get("language"))
        return plan_mod.clamp_language(detected, candidates)

    def transcribe_audio(
        self,
        media: Path,
        *,
        model: str,
        language: str | None,
        prompt: str | None,
    ) -> BackendTranscript:
        from . import plan as plan_mod

        plan = plan_mod.build_speech_plan(media, lang_candidates=())
        with TemporaryDirectory(prefix="immy-whispercpp-") as td:
            work = Path(td)
            if plan is not None and plan.regions:
                # Speech-region skip: slice each region, transcribe, offset back.
                region_wavs = plan_mod.materialize_region_wavs(
                    plan, work, sample_rate=self.sample_rate,
                )
                per_region: list[tuple[float, list[dict]]] = []
                lang_seen: str | None = None
                for offset_s, wav in region_wavs:
                    payload = self._inference(wav, language=language, prompt=prompt)
                    per_region.append((offset_s, self._segments(payload)))
                    lang_seen = lang_seen or payload.get("language")
                segments = plan_mod.merge_segments(per_region)
                detected = language or _lang_name_to_code(lang_seen)
            else:
                wav = plan_mod.materialize_wav(
                    media, work / "full.wav", sample_rate=self.sample_rate,
                )
                payload = self._inference(wav, language=language, prompt=prompt)
                segments = self._segments(payload)
                detected = language or _lang_name_to_code(payload.get("language"))
        text = " ".join(s["text"].strip() for s in segments if s["text"].strip())
        return BackendTranscript(
            segments=segments,
            text=text.strip(),
            language=detected or "",
        )


class QwenAsrBackend(WhisperCppBackend):
    """Qwen3-ASR (quality tier) over the SAME `/inference` verbose_json
    contract — served by the shim in `raw/qwen-asr-shim/` which wraps the
    `qwen3-asr-cpu` model in FastAPI. The wire format is identical to
    whisper-server, so the only difference is the name (and the endpoint the
    config points at). Qwen is the multilingual/no-hallucination champion;
    whisper.cpp stays the fast-EN option.
    """

    name = "qwen-asr"


__all__ = ["WhisperCppBackend", "QwenAsrBackend", "WhisperCppError"]
