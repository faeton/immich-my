"""WritablePaths resolver (Phase 6.1) — the write-path decoupling that lets
`immy process` run against a READ-ONLY originals mount on the NAS.

The load-bearing invariant: when both new roots are unset (the Mac path),
every resolved target is BYTE-IDENTICAL to the historical hardcoded layout.
The NAS path redirects state to state_root and sidecars to sidecars_root so
nothing lands under the (read-only) originals.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from immy.paths import resolve_writable_paths
from immy.state import AUDIT_DIR, Y_MARKER_FILENAME
from immy.journal import JOURNAL_FILENAME
from immy.heartbeat import HEARTBEAT_FILENAME


def test_resolve_writable_paths_defaults_match_current():
    """Unset roots → exactly the pre-refactor `<trip>/.audit/...` layout and
    sidecars beside the media. Pinned literally so a future edit can't drift
    the Mac path."""
    trip = Path("/Users/me/Incoming/2024-bali")
    p = resolve_writable_paths(trip)

    assert p.audit_dir == trip / AUDIT_DIR
    assert p.marker_path == trip / AUDIT_DIR / Y_MARKER_FILENAME
    assert p.journal_path == trip / AUDIT_DIR / JOURNAL_FILENAME
    assert p.heartbeat_path == trip / AUDIT_DIR / HEARTBEAT_FILENAME
    assert p.derivatives_dir == trip / AUDIT_DIR / "derivatives"
    assert p.offline_dir == trip / AUDIT_DIR / "offline"

    media = trip / "sub" / "clip.mov"
    # sidecars sit next to the media (== transcripts.sidecar_path / sidecar._sidecar_path)
    assert p.srt_path(media, "en") == trip / "sub" / "clip.en.srt"
    assert p.xmp_path(media) == trip / "sub" / "clip.xmp"


def test_resolve_writable_paths_nas_mode():
    """Roots set → state under state_root/<trip-rel>/.audit, sidecars mirror
    the media layout under sidecars_root/<trip-rel>."""
    originals = Path("/mnt/external/originals")
    trip = originals / "2024-bali"
    p = resolve_writable_paths(
        trip,
        originals_root=originals,
        state_root=Path("/scratch/immy-state"),
        sidecars_root=Path("/library/sidecars"),
    )

    assert p.marker_path == Path("/scratch/immy-state/2024-bali/.audit/y_processed.yml")
    assert p.journal_path == Path("/scratch/immy-state/2024-bali/.audit/journal.yml")
    assert p.derivatives_dir == Path("/scratch/immy-state/2024-bali/.audit/derivatives")

    media = trip / "sub" / "clip.mov"
    assert p.srt_path(media, "ru") == Path("/library/sidecars/2024-bali/sub/clip.ru.srt")
    assert p.xmp_path(media) == Path("/library/sidecars/2024-bali/sub/clip.xmp")


def test_nas_paths_all_outside_trip():
    """Every writable target must resolve OUTSIDE the read-only trip when
    roots are set — the whole point of the refactor."""
    originals = Path("/mnt/external/originals")
    trip = originals / "trip"
    p = resolve_writable_paths(
        trip,
        originals_root=originals,
        state_root=Path("/scratch"),
        sidecars_root=Path("/side"),
    )
    media = trip / "clip.mov"
    targets = [
        p.audit_dir, p.marker_path, p.journal_path, p.heartbeat_path,
        p.derivatives_dir, p.offline_dir,
        p.srt_path(media, "en"), p.xmp_path(media),
    ]
    for t in targets:
        assert trip not in t.parents and t != trip, f"{t} is under the :ro trip"


def test_trip_relative_fallback_when_not_under_originals_root():
    """If the trip isn't under originals_root, fall back to the trip's own
    name as <trip-rel> rather than crashing."""
    p = resolve_writable_paths(
        Path("/somewhere/else/trip"),
        originals_root=Path("/mnt/external/originals"),
        state_root=Path("/scratch"),
    )
    assert p.marker_path == Path("/scratch/trip/.audit/y_processed.yml")


def test_readonly_trip_writes_land_in_writable_roots(tmp_path: Path):
    """The real invariant under a chmod 0o555 trip: writing through the
    resolved NAS paths succeeds (lands under the writable roots) and the
    read-only trip dir is never mutated."""
    originals = tmp_path / "originals"
    trip = originals / "trip"
    trip.mkdir(parents=True)
    media = trip / "clip.mov"
    media.write_bytes(b"\x00")
    before = {p.name for p in trip.iterdir()}

    state_root = tmp_path / "state"
    sidecars_root = tmp_path / "sidecars"
    p = resolve_writable_paths(
        trip, originals_root=originals,
        state_root=state_root, sidecars_root=sidecars_root,
    )

    # Make the trip read-only (dir perms reject new entries).
    trip.chmod(0o555)
    try:
        for target in (p.marker_path, p.journal_path, p.heartbeat_path):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")
        p.derivatives_dir.mkdir(parents=True, exist_ok=True)
        (p.derivatives_dir / "thumb.jpg").write_bytes(b"\xff")
        srt = p.srt_path(media, "en")
        srt.parent.mkdir(parents=True, exist_ok=True)
        srt.write_text("1\n")
    finally:
        trip.chmod(0o755)  # restore so tmp cleanup can remove it

    # Trip dir untouched; everything landed under the writable roots.
    assert {p.name for p in trip.iterdir()} == before
    assert p.marker_path.is_file()
    assert (p.derivatives_dir / "thumb.jpg").is_file()
    assert p.srt_path(media, "en").is_file()
    assert state_root in p.marker_path.parents
    assert sidecars_root in p.srt_path(media, "en").parents


def test_srt_glob_finds_siblings(tmp_path: Path):
    """Default (no sidecars_root): srt_glob finds <stem>.*.srt next to media."""
    trip = tmp_path / "trip"
    trip.mkdir()
    media = trip / "clip.mov"
    media.write_bytes(b"\x00")
    (trip / "clip.en.srt").write_text("x")
    (trip / "clip.ru.srt").write_text("x")
    (trip / "other.en.srt").write_text("x")  # different stem — excluded
    p = resolve_writable_paths(trip)
    found = {q.name for q in p.srt_glob(media)}
    assert found == {"clip.en.srt", "clip.ru.srt"}
