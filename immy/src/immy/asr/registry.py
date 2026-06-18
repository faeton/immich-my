"""Backend selection (Phase 1).

`get_backend("mlx")` is the only wired backend today; "whispercpp" and
"qwen-asr" are reserved names that raise a clear NotImplementedError so a
config typo or premature use on the NAS fails loudly instead of silently
falling back to the Apple-only path.
"""

from __future__ import annotations

from .base import AsrBackend

KNOWN_BACKENDS = ("mlx", "whispercpp", "qwen-asr")


def get_backend(name: str = "mlx", **kwargs) -> AsrBackend:
    if name == "mlx":
        from .mlx_backend import MlxWhisperBackend
        return MlxWhisperBackend()
    if name in ("whispercpp", "qwen-asr"):
        raise NotImplementedError(
            f"whisper_backend {name!r} is not implemented yet "
            f"(Phase 2/5 — see raw/IMMY-ON-N5.md); only 'mlx' is wired."
        )
    raise ValueError(
        f"unknown whisper_backend {name!r}; expected one of {KNOWN_BACKENDS}"
    )


__all__ = ["get_backend", "KNOWN_BACKENDS"]
