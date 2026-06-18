"""ASR backend abstraction (Phase 1).

The pluggable inference layer for `transcripts.py`. Submodules are imported
lazily (never from this `__init__`) so `import immy.asr.types` stays free of
the heavy/Apple-only `mlx` import that `mlx_backend` pulls in.
"""
