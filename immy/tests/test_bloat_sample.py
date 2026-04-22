"""Unit tests for the sample/review pieces of the bloat module.

The ffmpeg-bound pieces (`_extract_frame`, `_overall_psnr`,
`sample_pair`) aren't exercised here — they'd need real video fixtures
and a local ffmpeg, and the regression risk is mostly in the glue +
rendering. We hit the glue instead: path inversion, Markdown
generation, verdict banding. An integration test with a tiny synthetic
MP4 would be nice but isn't worth the fixture weight today.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from immy import bloat as bloat_mod


def test_source_for_optimized_strips_suffix(tmp_path: Path) -> None:
    opt = tmp_path / "VID_0001.optimized.mp4"
    assert bloat_mod.source_for_optimized(opt).name == "VID_0001.mp4"


def test_source_for_optimized_is_inverse_of_optimized_path(tmp_path: Path) -> None:
    src = tmp_path / "clip.mov"
    opt = bloat_mod.optimized_path(src)
    assert bloat_mod.source_for_optimized(opt) == src


def test_source_for_optimized_no_marker_returns_same_path(tmp_path: Path) -> None:
    # Defensive: if the stem has no `.optimized` (user passed a raw
    # source path by mistake), don't rewrite it — return unchanged.
    plain = tmp_path / "VID_0001.mp4"
    assert bloat_mod.source_for_optimized(plain).name == "VID_0001.mp4"


def test_sample_report_verdict_ok() -> None:
    r = bloat_mod.SampleReport(
        source=Path("/tmp/a.mp4"), optimized=Path("/tmp/a.opt.mp4"),
        frames=[], psnr_db=42.5, review_dir=Path("/tmp"),
    )
    assert r.verdict == "ok"


def test_sample_report_verdict_review_band() -> None:
    r = bloat_mod.SampleReport(
        source=Path("/tmp/a.mp4"), optimized=Path("/tmp/a.opt.mp4"),
        frames=[], psnr_db=27.0, review_dir=Path("/tmp"),
    )
    assert r.verdict == "review"


def test_sample_report_verdict_fail_band() -> None:
    r = bloat_mod.SampleReport(
        source=Path("/tmp/a.mp4"), optimized=Path("/tmp/a.opt.mp4"),
        frames=[], psnr_db=22.0, review_dir=Path("/tmp"),
    )
    assert r.verdict == "fail"


def test_sample_report_verdict_unknown_when_psnr_missing() -> None:
    r = bloat_mod.SampleReport(
        source=Path("/tmp/a.mp4"), optimized=Path("/tmp/a.opt.mp4"),
        frames=[], psnr_db=None, review_dir=Path("/tmp"),
    )
    assert r.verdict == "unknown"


def test_psnr_thresholds_are_ordered_correctly() -> None:
    # Guards against someone swapping the constants during a refactor —
    # fail < review must always hold, or the banding collapses.
    assert bloat_mod.PSNR_FAIL_THRESHOLD < bloat_mod.PSNR_REVIEW_THRESHOLD


def test_render_review_md_writes_verdicts_and_references(tmp_path: Path) -> None:
    src = tmp_path / "VID_A.mp4"
    opt = tmp_path / "VID_A.optimized.mp4"
    src.write_bytes(b"")
    opt.write_bytes(b"")
    review_dir = tmp_path / "review"
    # Synthesize two frames that "exist" (render_review_md only needs
    # paths, not actual image bytes — the Markdown just references
    # them so a viewer can load them).
    frames = []
    for pct in (30, 70):
        f_src = review_dir / "VID_A" / f"{pct:03d}_src.jpg"
        f_opt = review_dir / "VID_A" / f"{pct:03d}_opt.jpg"
        f_src.parent.mkdir(parents=True, exist_ok=True)
        f_src.write_bytes(b"\xff\xd8\xff")  # JPEG magic; content irrelevant
        f_opt.write_bytes(b"\xff\xd8\xff")
        frames.append(bloat_mod.SampleFrame(
            percent=pct, timestamp=pct * 1.0,
            src_jpeg=f_src, opt_jpeg=f_opt,
        ))
    reports = [bloat_mod.SampleReport(
        source=src, optimized=opt,
        frames=frames, psnr_db=38.7, review_dir=review_dir / "VID_A",
    )]
    md = review_dir / "review.md"
    bloat_mod.render_review_md(reports, md)
    text = md.read_text()
    assert "VID_A.mp4" in text
    assert "38.70" in text
    assert "ok" in text
    # Relative image references resolve from review.md's directory.
    assert "VID_A/030_src.jpg" in text
    assert "VID_A/070_opt.jpg" in text


def test_render_review_md_handles_missing_frames(tmp_path: Path) -> None:
    # A pair where ffmpeg failed to extract frames still shows up in
    # the review with an explanatory placeholder instead of crashing.
    src = tmp_path / "broken.mp4"
    opt = tmp_path / "broken.optimized.mp4"
    review_dir = tmp_path / "review"
    reports = [bloat_mod.SampleReport(
        source=src, optimized=opt,
        frames=[], psnr_db=None, review_dir=review_dir / "broken",
    )]
    md = review_dir / "review.md"
    bloat_mod.render_review_md(reports, md)
    text = md.read_text()
    assert "unknown" in text
    assert "no frames extracted" in text
