"""Backend selection.

All three backends are wired: "mlx" (Apple, in-process; Phase 1), "whispercpp"
and "qwen-asr" (both HTTP to an `/inference` verbose_json server — whisper-server
and the Qwen shim respectively; Phase 2/5). The HTTP backends need `endpoint`.
An unknown name raises ValueError so a config typo fails loudly.
"""

from __future__ import annotations

from .base import AsrBackend

KNOWN_BACKENDS = ("mlx", "whispercpp", "qwen-asr")


def get_backend(name: str = "mlx", *, endpoint: str | None = None) -> AsrBackend:
    if name == "mlx":
        from .mlx_backend import MlxWhisperBackend
        return MlxWhisperBackend()
    if name == "whispercpp":
        from .whispercpp_backend import WhisperCppBackend
        return WhisperCppBackend(endpoint=endpoint or "")
    if name == "qwen-asr":
        from .whispercpp_backend import QwenAsrBackend
        return QwenAsrBackend(endpoint=endpoint or "")
    raise ValueError(
        f"unknown whisper_backend {name!r}; expected one of {KNOWN_BACKENDS}"
    )


__all__ = ["get_backend", "KNOWN_BACKENDS"]
