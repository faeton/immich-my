"""Cross-source dedup engine for the consolidation pipeline.

The one net-new piece of the iCloud + Google Takeout → Immich merge (see
raw/CONSOLIDATION-PIPELINE.md): dedup happens BEFORE import, Immich's own
Duplicate UI is second-pass QA only. Distinct from `duplicates.py`, which
answers "is this exact file already in Immich?" — this package answers
"are these two *different* files the same photo?" across resolutions,
formats, and sources.

Modules:
    manifest — manifest.sqlite ledger (status lifecycle, watermarks,
               embedding cache)
    phash    — 64-bit DCT perceptual hash (Stage B)
    engine   — fingerprint / block / cluster / decide (Stages A, B, D;
               Stage C CLIP-confirm lands after threshold calibration)
"""
