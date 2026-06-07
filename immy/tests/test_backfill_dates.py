from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from immy import backfill_dates as bf
from immy.exif import ExifRow
from immy.pg import LibraryInfo


LIB = LibraryInfo(id="lib-1", owner_id="owner-1", container_root="/data")


def _mock_conn(fetchone_return, rowcount: int = 1):
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = fetchone_return
    cur.rowcount = rowcount
    conn.cursor.return_value = cur
    return conn, cur


def _write_srt(path: Path, when: str = "2024-02-15 10:30:00") -> None:
    path.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{when}\n")


# --- resolve_capture ------------------------------------------------------


def test_resolve_capture_prefers_srt(tmp_path: Path) -> None:
    mov = tmp_path / "DJI_0001.MOV"
    mov.write_bytes(b"")
    _write_srt(tmp_path / "DJI_0001.SRT")
    row = ExifRow(path=mov, raw={"QuickTime:CreateDate": "2020:01:01 00:00:00"})
    dt, source, kind = bf.resolve_capture(mov, row)
    assert dt == datetime(2024, 2, 15, 10, 30, 0)  # SRT beat the embedded tag
    assert "SRT" in source
    assert kind == "utc"  # DJI SRT timestamps are UTC


def test_resolve_capture_filename_fallback(tmp_path: Path) -> None:
    mov = tmp_path / "DJI_20240309_141147_001.MOV"
    mov.write_bytes(b"")
    row = ExifRow(path=mov, raw={})  # no SRT, no embedded date
    dt, source, kind = bf.resolve_capture(mov, row)
    assert dt == datetime(2024, 3, 9, 14, 11, 47)
    assert "filename" in source
    assert kind == "local"  # filename stamp is local wall-clock


def test_resolve_capture_none(tmp_path: Path) -> None:
    mov = tmp_path / "clip.MOV"
    mov.write_bytes(b"")
    assert bf.resolve_capture(mov, ExifRow(path=mov, raw={})) is None


# --- _compute_instant -----------------------------------------------------


def test_compute_instant_utc_source_converts_to_local() -> None:
    # DJI SRT case: 03:32 UTC in Hawaii (UTC-10) is 17:32 the previous day.
    ldt, dto = bf._compute_instant(
        datetime(2023, 11, 26, 3, 32, 49), "utc", "Pacific/Honolulu",
    )
    assert dto == datetime(2023, 11, 26, 3, 32, 49, tzinfo=timezone.utc)
    assert ldt == datetime(2023, 11, 25, 17, 32, 49)  # localised wall clock


def test_compute_instant_utc_source_no_zone_keeps_utc_wall() -> None:
    ldt, dto = bf._compute_instant(
        datetime(2023, 11, 26, 3, 32, 49), "utc", None,
    )
    assert dto == datetime(2023, 11, 26, 3, 32, 49, tzinfo=timezone.utc)
    assert ldt == datetime(2023, 11, 26, 3, 32, 49)  # can't localise → UTC wall


def test_compute_instant_local_source_with_zone() -> None:
    # filename stamp: 10:30 local in Mauritius (UTC+4) is 06:30 UTC.
    ldt, dto = bf._compute_instant(
        datetime(2024, 2, 15, 10, 30, 0), "local", "Indian/Mauritius",
    )
    assert ldt == datetime(2024, 2, 15, 10, 30, 0)  # wall clock preserved
    assert dto == datetime(2024, 2, 15, 6, 30, 0, tzinfo=timezone.utc)


def test_compute_instant_local_source_no_zone() -> None:
    ldt, dto = bf._compute_instant(
        datetime(2024, 2, 15, 10, 30, 0), "local", None,
    )
    assert ldt == datetime(2024, 2, 15, 10, 30, 0)
    assert dto == datetime(2024, 2, 15, 10, 30, 0, tzinfo=timezone.utc)


# --- resolve_timezone -----------------------------------------------------


def test_resolve_timezone_override_validates(tmp_path: Path) -> None:
    tz, reason = bf.resolve_timezone([], tmp_path, "Europe/Riga")
    assert tz == "Europe/Riga"
    with pytest.raises(Exception):
        bf.resolve_timezone([], tmp_path, "Not/AZone")


def test_resolve_timezone_no_signal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bf, "guess_timezone", lambda rows, folder: None)
    monkeypatch.setattr(bf, "_tz_from_srt", lambda rows, folder: None)
    tz, reason = bf.resolve_timezone([], tmp_path, None)
    assert tz is None
    assert "wall clock" in reason.lower()


# --- plan_folder ----------------------------------------------------------


def _patch_read_folder(monkeypatch, rows):
    monkeypatch.setattr(bf, "read_folder", lambda folder: rows)


def test_plan_folder_builds_update_candidate(tmp_path: Path, monkeypatch) -> None:
    mov = tmp_path / "DJI_0001.MOV"
    mov.write_bytes(b"x")
    _write_srt(tmp_path / "DJI_0001.SRT")
    _patch_read_folder(monkeypatch, [ExifRow(path=mov, raw={})])
    # asset exists, exif row exists, date is NULL → update candidate
    conn, cur = _mock_conn(("asset-1", "asset-1", None))

    plan = bf.plan_folder(conn, LIB, tmp_path, tz_override="UTC")

    assert len(plan.candidates) == 1
    c = plan.candidates[0]
    assert c.asset_id == "asset-1"
    assert c.mode == "update"
    assert c.original_path == f"/data/{tmp_path.name}/DJI_0001.MOV"
    assert c.local_date_time == datetime(2024, 2, 15, 10, 30, 0)


def test_plan_folder_skips_already_dated(tmp_path: Path, monkeypatch) -> None:
    mov = tmp_path / "DJI_0002.MOV"
    mov.write_bytes(b"x")
    _write_srt(tmp_path / "DJI_0002.SRT")
    _patch_read_folder(monkeypatch, [ExifRow(path=mov, raw={})])
    existing = datetime(2024, 1, 1, tzinfo=timezone.utc)
    conn, cur = _mock_conn(("asset-2", "asset-2", existing))

    plan = bf.plan_folder(conn, LIB, tmp_path, tz_override="UTC")
    assert plan.candidates == []
    assert plan.already_dated == 1


def test_plan_folder_insert_mode_when_no_exif_row(tmp_path: Path, monkeypatch) -> None:
    mov = tmp_path / "DJI_0003.MOV"
    mov.write_bytes(b"x")
    _write_srt(tmp_path / "DJI_0003.SRT")
    _patch_read_folder(monkeypatch, [ExifRow(path=mov, raw={})])
    # asset exists but no exif row (exif_assetid NULL)
    conn, cur = _mock_conn(("asset-3", None, None))

    plan = bf.plan_folder(conn, LIB, tmp_path, tz_override="UTC")
    assert len(plan.candidates) == 1
    assert plan.candidates[0].mode == "insert"


def test_plan_folder_unmatched(tmp_path: Path, monkeypatch) -> None:
    mov = tmp_path / "DJI_0004.MOV"
    mov.write_bytes(b"x")
    _write_srt(tmp_path / "DJI_0004.SRT")
    _patch_read_folder(monkeypatch, [ExifRow(path=mov, raw={})])
    conn, cur = _mock_conn(None)  # no asset under that path

    plan = bf.plan_folder(conn, LIB, tmp_path, tz_override="UTC")
    assert plan.candidates == []
    assert plan.unmatched == [mov]


# --- apply_plan -----------------------------------------------------------


def _candidate(mode: str) -> bf.Candidate:
    return bf.Candidate(
        media_path=Path("/x/DJI_0001.MOV"), asset_id="a1",
        original_path="/data/t/DJI_0001.MOV", source="SRT", tz_name="UTC",
        local_date_time=datetime(2024, 2, 15, 10, 30, 0),
        date_time_original=datetime(2024, 2, 15, 10, 30, 0, tzinfo=timezone.utc),
        file_size=123, mode=mode,
    )


def test_apply_plan_writes_and_commits(tmp_path: Path) -> None:
    conn, cur = _mock_conn(None, rowcount=1)
    plan = bf.FolderPlan(folder=tmp_path, tz_name="UTC", tz_reason="x")
    plan.candidates = [_candidate("update")]

    written = bf.apply_plan(conn, plan)
    assert written == 1
    conn.commit.assert_called_once()
    # exif update + asset update
    assert cur.execute.call_count == 2
    assert "asset_exif" in cur.execute.call_args_list[0].args[0]
    assert "UPDATE asset" in cur.execute.call_args_list[1].args[0]


def test_apply_plan_skips_asset_when_exif_guard_blocks(tmp_path: Path) -> None:
    # exif UPDATE matched 0 rows (dated concurrently) → must NOT touch asset.
    conn, cur = _mock_conn(None, rowcount=0)
    plan = bf.FolderPlan(folder=tmp_path, tz_name="UTC", tz_reason="x")
    plan.candidates = [_candidate("update")]

    written = bf.apply_plan(conn, plan)
    assert written == 0
    assert cur.execute.call_count == 1  # only the guarded exif write ran
    conn.commit.assert_called_once()


def test_apply_plan_rolls_back_on_error(tmp_path: Path) -> None:
    conn, cur = _mock_conn(None, rowcount=1)
    cur.execute.side_effect = RuntimeError("boom")
    plan = bf.FolderPlan(folder=tmp_path, tz_name="UTC", tz_reason="x")
    plan.candidates = [_candidate("update")]

    with pytest.raises(RuntimeError):
        bf.apply_plan(conn, plan)
    conn.rollback.assert_called_once()
