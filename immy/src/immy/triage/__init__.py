"""Footage triage — grade trip videos keep/compress/cold/trash.

Phase 1 of shrinking the ~2.1T trip-video tier (2026-07): `scan` gathers
per-clip signals (ffprobe, sampled-frame CLIP, Immich favorite/album,
take-grouping) into `video_signal`; `report` ranks trips by recoverable
bytes. Human verdicts land in `triage` later (review-UI phase); nothing
in this package ever moves, rewrites, or re-encodes a file.
"""
