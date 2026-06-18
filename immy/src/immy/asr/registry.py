"""Backend selection (Phase 1).

`get_backend("mlx")` is the only wired backend today; "whispercpp" and
"qwen-asr" are reserved names that raise a clear NotImplementedError so a
config typo or premature use on the NAS fails loudly instead of silently
falling back to the Apple-only path.
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
        raise NotImplementedError(
            f"whisper_backend {name!r} is not implemented yet "
            f"(Phase 5 — see raw/IMMY-ON-N5.md); 'mlx' and 'whispercpp' are wired."
        )
    raise ValueError(
        f"unknown whisper_backend {name!r}; expected one of {KNOWN_BACKENDS}"
    )


__all__ = ["get_backend", "KNOWN_BACKENDS"]
