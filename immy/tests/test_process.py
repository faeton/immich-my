"""Tests for `immy process` (Phase Y.1): asset + asset_exif direct insert.

DB is mocked — we assert on the SQL/parameter shape, not on real writes.
A manual end-to-end against the NAS lives outside the unit tests.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from immy import process as process_mod
from immy import pg as pg_mod
from immy.cli import app
from immy.exif import read_folder
from immy.pg import LibraryInfo


FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()

LIB = LibraryInfo(
    id="lib-1",
    owner_id="owner-1",
    container_root="/mnt/external/originals",
)


# --- Pure helpers ---------------------------------------------------------


def test_path_checksum_matches_spec():
    # sha1("path:" + path) — 20 raw bytes, not hex.
    p = "/mnt/external/originals/trip/DSC_4182.JPG"
    expected = hashlib.sha1(f"path:{p}".encode()).digest()
    got = process_mod.path_checksum(p)
    assert got == expected
    assert len(got) == 20
    assert isinstance(got, bytes)


def test_path_checksum_deterministic_for_same_path():
    p = "/mnt/external/originals/x.jpg"
    assert process_mod.path_checksum(p) == process_mod.path_checksum(p)


def test_path_checksum_differs_for_different_paths():
    a = process_mod.path_checksum("/a/b.jpg")
    b = process_mod.path_checksum("/a/c.jpg")
    assert a != b


def test_container_path_for_anchors_under_root(tmp_path: Path):
    trip = tmp_path / "mauritius-2026"
    trip.mkdir()
    f = trip / "DJI_0001.JPG"
    f.write_bytes(b"x")
    cp = process_mod.container_path_for(f, trip, "/mnt/external/originals")
    assert cp == "/mnt/external/originals/mauritius-2026/DJI_0001.JPG"


def test_container_path_for_strips_trailing_slash(tmp_path: Path):
    trip = tmp_path / "t"
    trip.mkdir()
    f = trip / "a.jpg"
    f.write_bytes(b"x")
    cp = process_mod.container_path_for(f, trip, "/mnt/external/originals/")
    assert cp == "/mnt/external/originals/t/a.jpg"


def test_container_path_for_nested(tmp_path: Path):
    trip = tmp_path / "t"
    (trip / "sub").mkdir(parents=True)
    f = trip / "sub" / "a.jpg"
    f.write_bytes(b"x")
    cp = process_mod.container_path_for(f, trip, "/mnt/root")
    assert cp == "/mnt/root/t/sub/a.jpg"


def test_asset_type_for_image_vs_video():
    assert process_mod.asset_type_for(".JPG") == "IMAGE"
    assert process_mod.asset_type_for(".heic") == "IMAGE"
    assert process_mod.asset_type_for(".DNG") == "IMAGE"
    assert process_mod.asset_type_for(".mp4") == "VIDEO"
    assert process_mod.asset_type_for(".MOV") == "VIDEO"
    assert process_mod.asset_type_for(".insv") == "VIDEO"
    assert process_mod.asset_type_for(".lrv") == "VIDEO"


# --- build_rows -----------------------------------------------------------


def test_build_rows_dji_fixture_populates_exif(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    rows = read_folder(target)
    jpg_row = next(r for r in rows if r.path.suffix.upper() == ".JPG")

    asset, exif = process_mod.build_rows(jpg_row.path, target, jpg_row, LIB)

    assert asset.owner_id == "owner-1"
    assert asset.library_id == "lib-1"
    assert asset.device_id == "Library Import"
    assert asset.asset_type == "IMAGE"
    assert asset.original_path == f"/mnt/external/originals/{target.name}/DJI_0001.JPG"
    assert asset.original_file_name == "DJI_0001.JPG"
    assert asset.device_asset_id == "DJI_0001.JPG"
    assert len(asset.checksum) == 20
    assert asset.checksum == hashlib.sha1(
        f"path:{asset.original_path}".encode()
    ).digest()
    assert asset.duration is None  # image
    # Dates are populated & tz-aware UTC-anchored.
    assert asset.file_created_at.tzinfo is not None
    assert asset.file_modified_at.tzinfo is not None
    assert asset.local_date_time == asset.file_created_at

    assert exif.asset_id == asset.id
    assert exif.description == ""
    assert exif.file_size_in_byte == jpg_row.path.stat().st_size
    # Fixture JPEG is 1x1; width/height come from File group when EXIF-less.
    assert exif.exif_image_width == 1
    assert exif.exif_image_height == 1


def test_build_rows_deviceassetid_strips_spaces(tmp_path: Path):
    trip = tmp_path / "t"
    trip.mkdir()
    f = trip / "GP Temp Download.jpg"
    f.write_bytes(b"x")
    rows = read_folder(trip)
    asset, _ = process_mod.build_rows(f, trip, rows[0], LIB)
    assert asset.device_asset_id == "GPTempDownload.jpg"
    assert asset.original_file_name == "GP Temp Download.jpg"


def test_build_rows_uuid_is_unique(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    rows = read_folder(target)
    jpg_row = next(r for r in rows if r.path.suffix.upper() == ".JPG")
    a1, _ = process_mod.build_rows(jpg_row.path, target, jpg_row, LIB)
    a2, _ = process_mod.build_rows(jpg_row.path, target, jpg_row, LIB)
    # Each build_rows call mints a fresh UUID; dedupe happens in DB via checksum.
    assert a1.id != a2.id
    assert a1.checksum == a2.checksum


# --- _parse_exif_datetime ------------------------------------------------


def test_parse_exif_datetime_naive():
    dt = process_mod._parse_exif_datetime("2026:03:07 12:34:56")
    assert dt == datetime(2026, 3, 7, 12, 34, 56)
    assert dt.tzinfo is None


def test_parse_exif_datetime_with_offset():
    dt = process_mod._parse_exif_datetime("2026:03:07 12:34:56+04:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 4 * 3600


def test_parse_exif_datetime_garbage_returns_none():
    assert process_mod._parse_exif_datetime("not a date") is None
    assert process_mod._parse_exif_datetime(None) is None
    assert process_mod._parse_exif_datetime(123) is None


def test_has_date_rejects_sentinel_dates():
    """_has_date gates whether date-from-filename-vid-img fires. If a
    camera wrote `0000:00:00 00:00:00` into QuickTime:CreateDate the
    filename rule must still fire — otherwise process.py falls back to
    mtime, which on a Mac that just rsync'd the file means "now".
    """
    from immy.exif import ExifRow
    from immy.rules.dji_srt import _has_date
    from pathlib import Path as _P

    p = _P("/x/VID_20240219_133329.mp4")
    assert _has_date(ExifRow(path=p, raw={"QuickTime:CreateDate": "0000:00:00 00:00:00"})) is False
    assert _has_date(ExifRow(path=p, raw={"QuickTime:CreateDate": "1904:01:01 00:00:00"})) is False
    assert _has_date(ExifRow(path=p, raw={"QuickTime:CreateDate": "2024:02:19 13:33:29"})) is True
    assert _has_date(ExifRow(path=p, raw={})) is False


def test_parse_exif_datetime_rejects_bogus_year_bounds():
    # 0000:00:00 is strptime-invalid; 1969 is valid but pre-epoch; 2101 future.
    assert process_mod._parse_exif_datetime("0000:00:00 00:00:00") is None
    assert process_mod._parse_exif_datetime("1969:12:31 23:59:59") is None
    assert process_mod._parse_exif_datetime("2101:01:01 00:00:00") is None
    # Boundary accepts.
    assert process_mod._parse_exif_datetime("1970:01:01 00:00:00") is not None
    assert process_mod._parse_exif_datetime("2100:12:31 23:59:59") is not None


# --- insert_asset --------------------------------------------------------


def _fake_conn(asset_returning_id: str | None = "new-id") -> MagicMock:
    """Mock psycopg connection: tracks execute calls, returns given id from
    first fetchone() (the INSERT ... RETURNING id on the asset table).
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = (asset_returning_id,) if asset_returning_id else None
    conn.cursor.return_value = cur
    return conn, cur


def _make_rows(tmp_path: Path) -> tuple[process_mod.AssetRow, process_mod.AssetExifRow, Path]:
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    rows = read_folder(target)
    jpg_row = next(r for r in rows if r.path.suffix.upper() == ".JPG")
    asset, exif = process_mod.build_rows(jpg_row.path, target, jpg_row, LIB)
    return asset, exif, target


def test_insert_asset_executes_two_inserts_and_returns_true(tmp_path: Path):
    asset, exif, _ = _make_rows(tmp_path)
    conn, cur = _fake_conn("uuid-1")

    inserted = process_mod.insert_asset(conn, asset, exif)

    assert inserted is True
    # First call is asset INSERT, second is exif INSERT.
    assert cur.execute.call_count == 2
    sql_asset, params_asset = cur.execute.call_args_list[0].args
    sql_exif, params_exif = cur.execute.call_args_list[1].args
    assert "INSERT INTO asset" in sql_asset
    assert "'sha1-path'" in sql_asset
    assert "INSERT INTO asset_exif" in sql_exif
    assert params_asset["owner_id"] == "owner-1"
    assert params_asset["library_id"] == "lib-1"
    assert params_asset["asset_type"] == "IMAGE"
    assert params_exif["asset_id"] == asset.id
    assert params_exif["description"] == ""


def test_insert_asset_conflict_skips_exif(tmp_path: Path):
    asset, exif, _ = _make_rows(tmp_path)
    # RETURNING returns no row → checksum conflict on the asset insert;
    # we must NOT issue the exif INSERT (it'd dangle without a parent).
    # Follow-up SELECT resolves the existing id so caller code can key
    # derivatives off the real asset, not the ghost UUID build_rows made.
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = [None, ("real-existing-id",)]
    conn.cursor.return_value = cur

    ghost_id = asset.id
    inserted = process_mod.insert_asset(conn, asset, exif)

    assert inserted is False
    assert cur.execute.call_count == 2  # asset INSERT + SELECT for real id
    assert "INSERT INTO asset" in cur.execute.call_args_list[0].args[0]
    assert "SELECT id FROM asset" in cur.execute.call_args_list[1].args[0]
    assert asset.id == "real-existing-id" != ghost_id
    assert exif.asset_id == "real-existing-id"


# --- process_trip + marker -----------------------------------------------


def test_process_trip_builds_one_result_per_media_file(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur

    results = process_mod.process_trip(target, conn, LIB)

    assert len(results) == 1  # only DJI_0001.JPG is media; .SRT isn't
    assert results[0].inserted is True
    assert results[0].container_path.endswith("/DJI_0001.JPG")


def test_write_marker_drops_expected_yaml(tmp_path: Path):
    results = [
        process_mod.ProcessResult(asset_id="id-1", container_path="/x/a.jpg", inserted=True),
        process_mod.ProcessResult(asset_id="id-2", container_path="/x/b.jpg", inserted=False),
    ]
    marker = process_mod.write_marker(tmp_path, results)

    assert marker == tmp_path / ".audit" / "y_processed.yml"
    payload = yaml.safe_load(marker.read_text())
    assert payload["inserted"] == 1
    assert payload["already_present"] == 1
    assert len(payload["assets"]) == 2
    assert payload["assets"][0]["id"] == "id-1"


def test_is_processed_false_without_marker(tmp_path: Path):
    assert process_mod.is_processed(tmp_path) is False


def test_is_processed_true_with_marker(tmp_path: Path):
    process_mod.write_marker(tmp_path, [])
    assert process_mod.is_processed(tmp_path) is True


# --- Y.3 CLIP wiring -----------------------------------------------------


def _fake_derivative(preview_path: Path):
    """Build a DerivativeResult (thumbnail + preview pair + dims) whose
    preview staged_path exists on disk (so `process_trip` treats CLIP as
    runnable and the asset.width/height UPDATE has dims to write)."""
    from immy.derivatives import DerivativeFile, DerivativeResult

    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"fake preview")
    thumb = preview_path.with_name(preview_path.name.replace("_preview.jpeg", "_thumbnail.webp"))
    thumb.write_bytes(b"fake thumb")
    return DerivativeResult(
        files=[
            DerivativeFile(
                kind="thumbnail", staged_path=thumb,
                relative_path="thumbs/owner-1/aa/bb/id_thumbnail.webp",
                is_progressive=False, is_transparent=False,
            ),
            DerivativeFile(
                kind="preview", staged_path=preview_path,
                relative_path="thumbs/owner-1/aa/bb/id_preview.jpeg",
                is_progressive=True, is_transparent=False,
            ),
        ],
        width=4000, height=3000,
    )


def test_update_asset_dimensions_issues_single_update():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur

    process_mod.update_asset_dimensions(conn, "asset-xyz", 4000, 3000)

    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args.args
    assert "UPDATE asset" in sql
    assert "width" in sql and "height" in sql
    assert params == {"id": "asset-xyz", "width": 4000, "height": 3000}


def test_process_trip_writes_asset_width_height(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )

    process_mod.process_trip(
        target, conn, LIB, compute_derivatives=True, compute_clip=False,
    )

    update_sqls = [
        c.args for c in cur.execute.call_args_list if "UPDATE asset" in c.args[0]
    ]
    assert len(update_sqls) == 1
    params = update_sqls[0][1]
    # asset.id is a fresh uuid4 minted in build_rows — just sanity-check.
    assert params["width"] == 4000 and params["height"] == 3000
    assert isinstance(params["id"], str) and len(params["id"]) == 36


def test_process_trip_with_clip_upserts_smart_search(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur
    # fetch_smart_search_dim is a pg_mod function — stub at module level.
    monkeypatch.setattr(
        "immy.process.pg_mod.fetch_smart_search_dim",
        lambda c: 4,
    )
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )
    monkeypatch.setattr(
        "immy.process.clip_mod.embed_image",
        lambda path, model: [0.1, 0.2, 0.3, 0.4],
    )

    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=True,
    )

    assert len(results) == 1
    assert results[0].clip_embedded is True
    # One of the execute calls is the smart_search upsert.
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("INSERT INTO smart_search" in s for s in sqls), sqls


def test_process_trip_without_clip_never_queries_smart_search(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur

    called = {"dim": 0, "embed": 0}
    monkeypatch.setattr(
        "immy.process.pg_mod.fetch_smart_search_dim",
        lambda c: (called.__setitem__("dim", called["dim"] + 1), 512)[1],
    )
    monkeypatch.setattr(
        "immy.process.clip_mod.embed_image",
        lambda path, model: (called.__setitem__("embed", called["embed"] + 1), [0.0])[1],
    )

    results = process_mod.process_trip(target, conn, LIB, compute_clip=False)

    assert results[0].clip_embedded is False
    assert called == {"dim": 0, "embed": 0}


def test_process_trip_clip_requires_derivatives(tmp_path: Path):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    conn = MagicMock()
    with pytest.raises(ValueError, match="compute_derivatives"):
        process_mod.process_trip(
            target, conn, LIB,
            compute_derivatives=False, compute_clip=True,
        )


def test_process_trip_clip_dim_mismatch_soft_skips_by_default(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur
    monkeypatch.setattr("immy.process.pg_mod.fetch_smart_search_dim", lambda c: 512)
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )
    # 3-dim embedding but PG expects 512 → dim mismatch.
    monkeypatch.setattr("immy.process.clip_mod.embed_image", lambda p, m: [0.1, 0.2, 0.3])

    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=True,
    )
    assert results[0].clip_embedded is False  # soft skip
    # upsert never ran
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert not any("INSERT INTO smart_search" in s for s in sqls)


def test_process_trip_clip_dim_mismatch_raises_when_requested(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur
    monkeypatch.setattr("immy.process.pg_mod.fetch_smart_search_dim", lambda c: 512)
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )
    monkeypatch.setattr("immy.process.clip_mod.embed_image", lambda p, m: [0.1, 0.2])

    with pytest.raises(RuntimeError, match="CLIP dim mismatch"):
        process_mod.process_trip(
            target, conn, LIB,
            compute_derivatives=True, compute_clip=True,
            on_clip_error="raise",
        )


# --- Y.4 faces wiring -----------------------------------------------------


def _fake_detected(x1=10, y1=20, x2=60, y2=80, score=0.95):
    from immy.faces import DetectedFace
    return DetectedFace(x1=x1, y1=y1, x2=x2, y2=y2, score=score)


def _fake_embedded(face):
    from immy.faces import EmbeddedFace
    import numpy as np
    return EmbeddedFace(face=face, embedding=np.zeros(512, dtype=np.float32))


def test_process_trip_with_faces_writes_asset_face_rows(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur

    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )
    detected = [_fake_detected(), _fake_detected(x1=100, y1=110, x2=150, y2=160)]
    monkeypatch.setattr(
        "immy.process.faces_mod.detect",
        lambda b: (detected, 1440, 960),
    )
    monkeypatch.setattr(
        "immy.process.faces_mod.embed_faces",
        lambda b, faces, model: [_fake_embedded(f) for f in faces],
    )

    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=False, compute_faces=True,
    )

    assert results[0].faces_detected == 2
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("DELETE FROM asset_face" in s for s in sqls), sqls
    assert sum("INSERT INTO asset_face" in s for s in sqls) == 2
    assert sum("INSERT INTO face_search" in s for s in sqls) == 2


def test_process_trip_without_faces_never_calls_detector(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur

    called = {"detect": 0}
    monkeypatch.setattr(
        "immy.process.faces_mod.detect",
        lambda b: (called.__setitem__("detect", called["detect"] + 1), ([], 0, 0))[1],
    )
    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )

    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=False, compute_faces=False,
    )
    assert results[0].faces_detected == 0
    assert called["detect"] == 0


def test_process_trip_faces_soft_skip_on_error(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur

    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )

    def boom(_b):
        raise RuntimeError("vision exploded")

    monkeypatch.setattr("immy.process.faces_mod.detect", boom)

    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=False, compute_faces=True,
    )
    assert results[0].faces_detected == 0  # soft-skip


def test_process_trip_faces_skips_when_no_detections(tmp_path: Path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    conn.cursor.return_value = cur

    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset",
        lambda **kw: _fake_derivative(
            tmp_path / "preview-out" / f"{kw['asset_id']}_preview.jpeg"
        ),
    )
    monkeypatch.setattr("immy.process.faces_mod.detect", lambda b: ([], 1440, 960))
    # embed must not be called when detect returns empty
    monkeypatch.setattr(
        "immy.process.faces_mod.embed_faces",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    results = process_mod.process_trip(
        target, conn, LIB,
        compute_derivatives=True, compute_clip=False, compute_faces=True,
    )
    assert results[0].faces_detected == 0
    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert not any("INSERT INTO asset_face" in s for s in sqls)


def test_write_marker_records_faces_count(tmp_path: Path):
    results = [
        process_mod.ProcessResult(
            asset_id="id-1", container_path="/x/a.jpg", inserted=True,
            faces_detected=3,
        ),
        process_mod.ProcessResult(
            asset_id="id-2", container_path="/x/b.jpg", inserted=True,
        ),
    ]
    marker = process_mod.write_marker(tmp_path, results)
    payload = yaml.safe_load(marker.read_text())
    assert payload["faces_detected"] == 3
    assert payload["assets"][0].get("faces_detected") == 3
    assert "faces_detected" not in payload["assets"][1]


def test_write_marker_records_clip_embedded_count(tmp_path: Path):
    results = [
        process_mod.ProcessResult(
            asset_id="id-1", container_path="/x/a.jpg", inserted=True,
            clip_embedded=True,
        ),
        process_mod.ProcessResult(
            asset_id="id-2", container_path="/x/b.jpg", inserted=True,
            clip_embedded=False,
        ),
    ]
    marker = process_mod.write_marker(tmp_path, results)
    payload = yaml.safe_load(marker.read_text())
    assert payload["clip_embedded"] == 1
    assert payload["assets"][0].get("clip_embedded") is True
    assert "clip_embedded" not in payload["assets"][1]


# --- CLI driver -----------------------------------------------------------


@pytest.fixture
def config_full(tmp_path: Path, monkeypatch) -> Path:
    originals = tmp_path / "originals"
    originals.mkdir()
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump({
            "originals_root": str(originals),
            "immich": {"url": "http://fake", "api_key": "k", "library_id": "lib-1"},
            "pg": {
                "host": "127.0.0.1", "port": 15432,
                "user": "postgres", "password": "x", "database": "immich",
            },
        })
    )
    monkeypatch.setenv("IMMY_CONFIG", str(cfg))
    return cfg


def test_process_cli_dry_run_no_pg_connect(config_full, tmp_path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    # Stub connect + fetch so dry-run doesn't hit real DB.
    fake_conn = MagicMock()
    monkeypatch.setattr(pg_mod, "connect", lambda cfg: fake_conn)
    monkeypatch.setattr("immy.cli.pg_mod.connect", lambda cfg: fake_conn)
    monkeypatch.setattr(
        "immy.cli.pg_mod.fetch_library_info",
        lambda conn, lib_id: LIB,
    )

    result = runner.invoke(app, ["process", str(target), "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "dry-run" in result.stdout
    assert "would process" in result.stdout
    # No INSERT attempted.
    fake_conn.cursor.assert_not_called()
    # No marker written on dry-run.
    assert not (target / ".audit" / "y_processed.yml").exists()


def test_process_cli_inserts_and_drops_marker(config_full, tmp_path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)

    fake_conn = MagicMock()
    fake_conn.closed = False
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("uuid-x",)
    fake_conn.cursor.return_value = cur

    monkeypatch.setattr("immy.cli.pg_mod.connect", lambda cfg: fake_conn)
    monkeypatch.setattr(
        "immy.cli.pg_mod.fetch_library_info",
        lambda conn, lib_id: LIB,
    )

    result = runner.invoke(app, ["process", str(target)])
    assert result.exit_code == 0, result.stdout
    assert "1 new asset" in result.stdout
    assert (target / ".audit" / "y_processed.yml").is_file()
    fake_conn.commit.assert_called_once()


def test_process_cli_errors_without_pg_config(tmp_path, monkeypatch):
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    # Config without pg:
    cfg = tmp_path / "no-pg.yml"
    cfg.write_text(
        yaml.safe_dump({
            "originals_root": str(tmp_path),
            "immich": {"url": "http://fake", "api_key": "k", "library_id": "lib-1"},
        })
    )
    monkeypatch.setenv("IMMY_CONFIG", str(cfg))
    result = runner.invoke(app, ["process", str(target)])
    assert result.exit_code == 2
    assert "no pg:" in result.stdout


def test_promote_skips_scan_when_marker_present(config_full, tmp_path, monkeypatch):
    """With `.audit/y_processed.yml`, promote must NOT POST to /scan."""
    from immy import immich as immich_mod
    from immy import promote as promote_mod

    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    # Audit first so no HIGH pending, then drop the Y marker.
    runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    process_mod.write_marker(target, [process_mod.ProcessResult(
        asset_id="id-1", container_path="/x/a.jpg", inserted=True,
    )])

    class FakeClient:
        def __init__(self, **kw):
            self.scans: list[str] = []
        def scan_library(self, library_id):
            self.scans.append(library_id)
        def find_asset_id(self, name):
            return None
        def create_stack(self, *a, **kw):
            return None
        def find_album_by_name(self, name):
            return None
        def create_album(self, *a, **kw):
            return "alb-1"
        def update_album(self, *a, **kw):
            pass
        def add_assets_to_album(self, album_id, asset_ids):
            return []

    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: None)

    result = runner.invoke(app, ["promote", str(target)])
    assert result.exit_code == 0, result.stdout
    assert fake.scans == []  # the whole point
    assert "scan skipped" in result.stdout
