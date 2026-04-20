"""Tests for `immy process --with-derivatives` + `immy promote` push
(Phase Y.2).

Covers: pyvips output dimensions, staged path layout, marker extension,
promote's rsync + asset_file INSERT flow. No real DB or rsync — both
are faked.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pyvips
import yaml

from immy import derivatives as derivatives_mod
from immy import process as process_mod
from immy import promote as promote_mod
from immy.config import Config, ImmichConfig, MediaConfig, PgConfig
from immy.derivatives import DerivativeFile
from immy.pg import LibraryInfo


LIB = LibraryInfo(
    id="lib-1",
    owner_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    container_root="/mnt/external/originals",
)


def _make_png(dst: Path, width: int = 1000, height: int = 800) -> None:
    """Write a minimal valid PNG so pyvips can decode without extra deps."""
    img = pyvips.Image.black(width, height, bands=3)
    img = img.linear([0, 0, 0], [128, 64, 200])  # solid colour
    img.pngsave(str(dst))


# --- relative_path_for ----------------------------------------------------


def test_relative_path_bucketing_matches_immich_layout():
    asset_id = "abc12345-6789-4def-89ab-cdef01234567"
    owner = "owner-uuid"
    thumb = derivatives_mod.relative_path_for(asset_id, owner, "thumbnail")
    preview = derivatives_mod.relative_path_for(asset_id, owner, "preview")
    assert thumb == f"thumbs/{owner}/ab/c1/{asset_id}_thumbnail.webp"
    assert preview == f"thumbs/{owner}/ab/c1/{asset_id}_preview.jpeg"


def test_relative_path_uses_first_four_chars_of_id():
    # Two-level 2+2 bucketing — not 2+4 or 4+4.
    p = derivatives_mod.relative_path_for("12345678-rest", "u", "thumbnail")
    assert "/12/34/" in p


# --- compute_for_asset ----------------------------------------------------


def test_compute_writes_thumbnail_and_preview(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    src = trip / "IMG_0001.png"
    _make_png(src)

    derivs = derivatives_mod.compute_for_asset(
        source_media=src,
        asset_id="abcd1234-ffff-4000-8000-000000000000",
        owner_id=LIB.owner_id,
        asset_type="IMAGE",
        trip_folder=trip,
    )
    assert len(derivs) == 2
    kinds = {d.kind for d in derivs}
    assert kinds == {"thumbnail", "preview"}

    for d in derivs:
        assert d.staged_path.is_file()
        assert str(d.staged_path).endswith(d.relative_path)
        # Staged under .audit/derivatives/thumbs/...
        rel_to_trip = d.staged_path.relative_to(trip).as_posix()
        assert rel_to_trip.startswith(".audit/derivatives/thumbs/")


def test_compute_thumbnail_width_is_250(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    src = trip / "a.png"
    _make_png(src, width=2000, height=1500)

    derivs = derivatives_mod.compute_for_asset(
        source_media=src,
        asset_id="abcd1234-ffff-4000-8000-000000000000",
        owner_id="u",
        asset_type="IMAGE",
        trip_folder=trip,
    )
    thumb = next(d for d in derivs if d.kind == "thumbnail")
    preview = next(d for d in derivs if d.kind == "preview")
    t_img = pyvips.Image.new_from_file(str(thumb.staged_path))
    p_img = pyvips.Image.new_from_file(str(preview.staged_path))
    assert t_img.width == derivatives_mod.THUMBNAIL_WIDTH
    assert p_img.width == derivatives_mod.PREVIEW_WIDTH
    # Preview marked progressive (matters for asset_file.isProgressive).
    assert preview.is_progressive is True
    assert thumb.is_progressive is False


def test_compute_skips_videos(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    src = trip / "v.mp4"
    src.write_bytes(b"not a real video")

    derivs = derivatives_mod.compute_for_asset(
        source_media=src,
        asset_id="id",
        owner_id="u",
        asset_type="VIDEO",
        trip_folder=trip,
    )
    assert derivs == []


# --- process_trip integration --------------------------------------------


def _fake_cursor() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("new-id",)
    conn.cursor.return_value = cur
    return conn, cur


def test_process_trip_with_derivatives_stages_files(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    src = trip / "IMG_0001.png"
    _make_png(src)

    conn, cur = _fake_cursor()
    results = process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True,
    )

    assert len(results) == 1
    r = results[0]
    assert r.derivatives is not None
    assert len(r.derivatives) == 2
    # Files really exist on disk.
    for d in r.derivatives:
        assert d.staged_path.is_file()


def test_process_trip_skips_derivatives_for_existing_asset(tmp_path: Path):
    """On checksum conflict (already_present), no new derivative work."""
    trip = tmp_path / "trip"
    trip.mkdir()
    src = trip / "IMG_0001.png"
    _make_png(src)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = None  # conflict
    conn.cursor.return_value = cur

    results = process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True,
    )
    assert results[0].inserted is False
    assert results[0].derivatives is None
    # No files staged either.
    staged = trip / ".audit" / "derivatives"
    assert not staged.exists() or not any(staged.rglob("*.webp"))


def test_write_marker_records_derivatives(tmp_path: Path):
    results = [
        process_mod.ProcessResult(
            asset_id="id-1",
            container_path="/x/a.jpg",
            inserted=True,
            asset_type="IMAGE",
            derivatives=[
                DerivativeFile(
                    kind="thumbnail",
                    staged_path=tmp_path / ".audit/derivatives/thumbs/u/id/-1/id-1_thumbnail.webp",
                    relative_path="thumbs/u/id/-1/id-1_thumbnail.webp",
                    is_progressive=False,
                    is_transparent=False,
                ),
                DerivativeFile(
                    kind="preview",
                    staged_path=tmp_path / ".audit/derivatives/thumbs/u/id/-1/id-1_preview.jpeg",
                    relative_path="thumbs/u/id/-1/id-1_preview.jpeg",
                    is_progressive=True,
                    is_transparent=False,
                ),
            ],
        ),
    ]
    marker = process_mod.write_marker(tmp_path, results)
    payload = yaml.safe_load(marker.read_text())
    assert payload["derivatives_staged"] == 2
    derivs = payload["assets"][0]["derivatives"]
    assert {d["kind"] for d in derivs} == {"thumbnail", "preview"}
    assert derivs[1]["is_progressive"] is True


def test_read_marker_returns_none_without_file(tmp_path: Path):
    assert process_mod.read_marker(tmp_path) is None


def test_read_marker_roundtrip(tmp_path: Path):
    process_mod.write_marker(tmp_path, [process_mod.ProcessResult(
        asset_id="id-x", container_path="/x", inserted=True,
    )])
    got = process_mod.read_marker(tmp_path)
    assert got is not None
    assert got["assets"][0]["id"] == "id-x"


# --- promote _push_derivatives -------------------------------------------


def _cfg(host_root: str, container_root: str = "/data") -> Config:
    return Config(
        originals_root=Path("/tmp"),
        immich=ImmichConfig(url="http://fake", api_key="k", library_id="lib-1"),
        pg=PgConfig(host="h", port=15432, user="u", password="p", database="d"),
        media=MediaConfig(host_root=host_root, container_root=container_root),
        notes_filename=None,
        source=None,
    )


def _marker_with_derivs(trip: Path, asset_id: str = "id-1") -> None:
    rel_thumb = f"thumbs/u/ab/cd/{asset_id}_thumbnail.webp"
    rel_prev = f"thumbs/u/ab/cd/{asset_id}_preview.jpeg"
    payload = {
        "processed_at": 0, "inserted": 1, "already_present": 0,
        "derivatives_staged": 2,
        "assets": [{
            "id": asset_id,
            "file": "/mnt/external/originals/t/a.jpg",
            "new": True,
            "type": "IMAGE",
            "derivatives": [
                {"kind": "thumbnail", "relative_path": rel_thumb,
                 "is_progressive": False, "is_transparent": False},
                {"kind": "preview", "relative_path": rel_prev,
                 "is_progressive": True, "is_transparent": False},
            ],
        }],
    }
    (trip / ".audit").mkdir(parents=True, exist_ok=True)
    (trip / ".audit" / "y_processed.yml").write_text(yaml.safe_dump(payload))
    # Also stage placeholder files so promote's existence check passes.
    for rel in (rel_thumb, rel_prev):
        p = trip / ".audit" / "derivatives" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_push_derivatives_rsyncs_and_inserts(tmp_path: Path, monkeypatch):
    trip = tmp_path / "trip"
    trip.mkdir()
    _marker_with_derivs(trip, asset_id="id-1")

    # Capture rsync call + DB calls.
    rsync_calls: list[list[str]] = []
    def fake_run(args, **kw):
        rsync_calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(promote_mod.subprocess, "run", fake_run)

    fake_conn = MagicMock()
    fake_conn.closed = False
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    fake_conn.cursor.return_value = cur
    monkeypatch.setattr(promote_mod.pg_mod, "connect", lambda cfg: fake_conn)

    plan = promote_mod.Plan(
        folder=trip, target=Path("/tmp/out"), pairs=[], pending_high=0,
    )
    cfg = _cfg("/volume1/faeton-immi/library")

    summary = promote_mod._push_derivatives(plan, cfg)

    assert summary is not None
    assert summary["status"] == "pushed"
    assert summary["rows_written"] == 2
    # Rsync source points at `.audit/derivatives/`, dst at host_root.
    assert any("derivatives" in a for a in rsync_calls[0])
    assert rsync_calls[0][-1].startswith("/volume1/faeton-immi/library")
    # Two asset_file INSERTs with the expected container_root prefix.
    assert cur.execute.call_count == 2
    params = [c.args[1] for c in cur.execute.call_args_list]
    types = [p["type"] for p in params]
    paths = [p["path"] for p in params]
    assert sorted(types) == ["preview", "thumbnail"]
    assert all(p.startswith("/data/thumbs/u/ab/cd/id-1_") for p in paths)
    fake_conn.commit.assert_called_once()


def test_push_derivatives_none_without_marker(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    plan = promote_mod.Plan(folder=trip, target=Path("/tmp"), pairs=[], pending_high=0)
    assert promote_mod._push_derivatives(plan, _cfg("/x")) is None


def test_push_derivatives_skipped_without_media_config(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    _marker_with_derivs(trip)
    cfg = Config(
        originals_root=Path("/tmp"),
        immich=None, pg=None, media=None,
        notes_filename=None, source=None,
    )
    plan = promote_mod.Plan(folder=trip, target=Path("/tmp"), pairs=[], pending_high=0)
    summary = promote_mod._push_derivatives(plan, cfg)
    assert summary["status"] == "skipped"


def test_push_derivatives_rsync_error_reports_detail(tmp_path: Path, monkeypatch):
    trip = tmp_path / "trip"
    trip.mkdir()
    _marker_with_derivs(trip)

    def fake_run(args, **kw):
        raise subprocess.CalledProcessError(23, args, output="", stderr="boom")
    monkeypatch.setattr(promote_mod.subprocess, "run", fake_run)

    plan = promote_mod.Plan(folder=trip, target=Path("/tmp"), pairs=[], pending_high=0)
    summary = promote_mod._push_derivatives(plan, _cfg("/dest"))
    assert summary["status"] == "error"
    assert "boom" in summary["detail"]
