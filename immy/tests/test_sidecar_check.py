"""Sidecars that contradict their originals: the rules, and `immy sidecars check`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immy import cli
from immy import sidecar_check as sc

UTC = timezone.utc


def _xmp(dto=None, lat=None, lon=None) -> str:
    parts = []
    if dto:
        parts.append(f"<exif:DateTimeOriginal>{dto}</exif:DateTimeOriginal>")
    for tag, v, pos, neg in (("GPSLatitude", lat, "N", "S"), ("GPSLongitude", lon, "E", "W")):
        if v is not None:
            d = int(abs(v))
            parts.append(f"<exif:{tag}>{d},{(abs(v) - d) * 60:.4f}{pos if v >= 0 else neg}</exif:{tag}>")
    return "<x:xmpmeta><rdf:Description>" + "".join(parts) + "</rdf:Description></x:xmpmeta>"


# --- reading ---------------------------------------------------------------------


def test_file_facts_reads_apple_meta_and_stills() -> None:
    apple = sc.file_facts({"GPSLatitude": 36.13, "GPSLongitude": -115.17,
                           "CreateDate": "2023:11:30 23:08:04",
                           "CreationDate": "2023:11:30 15:08:04-08:00"}, "IMG_3915.MOV")
    assert apple.video and apple.lon == -115.17
    assert apple.local.isoformat() == "2023-11-30T15:08:04-08:00"
    assert apple.utc == datetime(2023, 11, 30, 23, 8, 4, tzinfo=UTC)
    meta = sc.file_facts({"CreationDate": "2026:05:10 04:52:29Z",
                          "CreateDate": "2026:05:11 18:11:46"}, "mcp_video-1.mov")
    assert meta.local is None                       # Z is UTC, not a local clock
    assert meta.utc == datetime(2026, 5, 10, 4, 52, 29, tzinfo=UTC)
    still = sc.file_facts({"DateTimeOriginal": "2024:03:07 17:39:46",
                           "OffsetTimeOriginal": "-03:00"}, "x.jpg")
    assert not still.video and still.local.utcoffset() == timedelta(hours=-3)
    assert still.utc is None
    assert sc.file_facts({"GPSLatitude": 0, "GPSLongitude": 0}, "a.mov").lat is None


def test_read_sidecar_keeps_the_hemisphere() -> None:
    s = sc.read_sidecar(_xmp("2025-10-08T03:29:23", -17.803, 177.4154))
    assert s.dto == "2025-10-08T03:29:23"
    assert s.lat == pytest.approx(-17.803, abs=1e-4) and s.lon == pytest.approx(177.4154, abs=1e-4)
    attr = sc.read_sidecar("<rdf:Description exif:GPSLatitude='17,48.18S' "
                           "exif:GPSLongitude='149,34.2W' exif:DateTimeOriginal='2025-10-24T08:00:00'/>")
    assert attr.lat == pytest.approx(-17.803) and attr.lon == pytest.approx(-149.57)
    assert attr.dto == "2025-10-24T08:00:00"


# --- rules -----------------------------------------------------------------------

VEGAS = sc.FileFacts(36.1322, -115.1661, datetime.fromisoformat("2023-11-30T15:08:04-08:00"),
                     datetime(2023, 11, 30, 23, 8, 4, tzinfo=UTC), True)


def test_a_sign_lost_against_the_files_own_gps_is_restored() -> None:
    side = sc.SidecarFacts("2023-11-30T23:08:04", 36.1322, 115.1661)   # "China"
    fix = sc.plan(VEGAS, side)
    assert fix.reasons == ["gps-sign", "clock"]
    assert fix.patch["GPSLongitude"] == -115.1661 and fix.patch["GPSLongitudeRef"] == "W"
    assert fix.patch["DateTimeOriginal"] == "2023:11:30 15:08:04-08:00"


def test_a_correct_sidecar_is_left_alone() -> None:
    assert sc.plan(VEGAS, sc.SidecarFacts("2023:11:30 15:08:04-08:00", 36.1322, -115.1661)) is None
    # A still's local wall time without an offset is right as it is.
    still = sc.FileFacts(local=datetime.fromisoformat("2024-03-07T17:39:46-03:00"))
    assert sc.plan(still, sc.SidecarFacts("2024-03-07T17:39:46")) is None


def test_a_deliberate_correction_is_not_undone() -> None:
    # Location moved in Photos (km away, not a mirror) and a camera clock
    # fixed by hours and minutes: neither is the bug's signature.
    side = sc.SidecarFacts("2023-11-30T19:34:12", 36.20, -115.30)
    assert sc.plan(VEGAS, side) is None
    still = sc.FileFacts(local=datetime.fromisoformat("2024-03-07T17:39:46-03:00"))
    assert sc.plan(still, sc.SidecarFacts("2024-03-07T22:05:54")) is None


def test_sidecar_only_gps_is_unflipped_by_the_shots_around_it() -> None:
    meta = sc.FileFacts(utc=datetime(2025, 10, 7, 15, 29, 23, tzinfo=UTC), video=True)
    side = sc.SidecarFacts("2025-10-07T15:29:23", 17.803, 177.4154)        # 17.8 N
    hint = sc.Hint(-17.75, 177.45, None)                                   # Fiji phone shot
    fix = sc.plan(meta, side, hint)
    assert fix.reasons == ["gps-sign", "clock-utc"]
    assert fix.patch["GPSLatitude"] == -17.803
    assert fix.patch["DateTimeOriginal"] == "2025:10:08 03:29:23+12:00"    # Fiji time
    # Without a hint it can't tell, and leaves the position.
    assert "GPSLatitude" not in (sc.plan(meta, side) or sc.Repair()).patch


def test_a_bare_utc_clock_takes_the_zone_of_the_shots_around_it() -> None:
    from zoneinfo import ZoneInfo
    meta = sc.FileFacts(utc=datetime(2026, 5, 10, 4, 52, 29, tzinfo=UTC), video=True)
    fix = sc.plan(meta, sc.SidecarFacts("2026-05-10T04:52:29"), sc.Hint(zone=ZoneInfo("Asia/Kolkata")))
    assert fix.patch == {"DateTimeOriginal": "2026:05:10 10:22:29+05:30"}
    assert sc.plan(meta, sc.SidecarFacts("2026-05-10T04:52:29")) is None   # no zone: no guess


def test_promote_guard_fixes_the_patch_before_it_is_written() -> None:
    patch = {"DateTimeOriginal": "2023:11:30 23:08:04", "GPSLatitude": 36.1322,
             "GPSLatitudeRef": "N", "GPSLongitude": 115.1661, "GPSLongitudeRef": "E"}
    out = sc.correct_patch(patch, VEGAS)
    assert out["GPSLongitude"] == -115.1661 and out["GPSLongitudeRef"] == "W"
    assert out["DateTimeOriginal"] == "2023:11:30 15:08:04-08:00"
    good = {"DateTimeOriginal": "2023:11:30 15:08:04-08:00"}
    assert sc.correct_patch(good, VEGAS) == good


def test_rescue_sidecar_runs_the_guard(tmp_path, monkeypatch) -> None:
    from immy.dedup import engine
    written = []
    monkeypatch.setattr(engine, "_own_metadata", lambda p: {
        "GPSLatitude": 36.1322, "GPSLongitude": -115.1661, "CreateDate": "2023:11:30 23:08:04",
        "CreationDate": "2023:11:30 15:08:04-08:00"})
    monkeypatch.setattr(engine.sidecar, "write", lambda dest, patch, **kw: written.append(patch))
    assert engine._rescue_sidecar(tmp_path / "IMG_3915.MOV", "2023-11-30T23:08:04", 36.1322, 115.1661)
    assert written[0]["GPSLongitude"] == -115.1661
    assert written[0]["DateTimeOriginal"] == "2023:11:30 15:08:04-08:00"


# --- CLI -------------------------------------------------------------------------


class _Cur:
    def __init__(self, db):
        self.db, self._rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if 'FROM "user"' in sql:
            self._rows = [("u1", "me@example.com")]
        elif '"importPaths"' in sql:
            self._rows = [(["/lib"],)]
        elif sql is sc.SIDECARS_SQL:
            self._rows = self.db["sidecars"]
        elif sql is sc.UTC_CLOCK_SQL:
            self._rows = self.db.get("utc", [])
        elif sql is sc.REGISTER_SIDECAR_SQL:
            self.db.setdefault("registered", []).append((params["asset"], params["path"]))
            self._rows = []
        elif sql is sc.SHARED_OWNERS_SQL:
            self._rows = [(x,) for x in params["paths"] if x in self.db.get("cross", ())]
        elif sql is sc.HINTS_SQL:
            self.db["hint_calls"].append(sorted(params["ids"]))
            self._rows = [(i, *self.db["hints"].get(i, (None, None, None))) for i in params["ids"]]
        elif "FROM asset_file" in sql:
            self._rows = [(a, s) for a, _, s, _ in self.db["sidecars"]]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cur(self.db)

    def close(self):
        pass

    def commit(self):
        pass


class _Api:
    refreshed: list = []
    down = False

    def __init__(self, **kw):
        pass

    def refresh_metadata(self, ids):
        if _Api.down:
            raise RuntimeError("immich down")
        _Api.refreshed.append(sorted(ids))


@pytest.fixture
def lib(tmp_path, monkeypatch, no_schema_guard):
    """A library of three videos with name.xmp sidecars: Vegas (sign + UTC
    clock, file knows the truth), Fiji Meta (sidecar-only GPS, bare UTC), and
    a correct one."""
    root = tmp_path / "originals"
    (root / "v").mkdir(parents=True)
    files = {
        "vegas": ("IMG_3915.MOV", _xmp("2023-11-30T23:08:04", 36.1322, 115.1661),
                  {"GPSLatitude": 36.1322, "GPSLongitude": -115.1661, "CreateDate": "2023:11:30 23:08:04",
                   "CreationDate": "2023:11:30 15:08:04-08:00"}),
        "fiji": ("mcp_video-1.MOV", _xmp("2025-10-07T15:29:23", 17.803, 177.4154),
                 {"CreationDate": "2025:10:07 15:29:23Z"}),
        "ok": ("IMG_1.MOV", _xmp("2024:01:01 10:00:00+01:00", 52.2, 21.0),
               {"GPSLatitude": 52.2, "GPSLongitude": 21.0, "CreationDate": "2024:01:01 10:00:00+01:00",
                "CreateDate": "2024:01:01 09:00:00"}),
    }
    sidecars, exif = [], []
    for aid, (name, xmp, meta) in files.items():
        (root / "v" / name).write_bytes(b"")
        (root / "v" / (name.rsplit(".", 1)[0] + ".xmp")).write_text(xmp)
        sidecars.append((aid, f"/lib/v/{name}", f"/lib/v/{name.rsplit('.', 1)[0]}.xmp",
                         datetime(2025, 1, 1, tzinfo=UTC)))
        exif.append({"SourceFile": str(root / "v" / name), **meta})
    db = {"sidecars": sidecars, "hints": {"fiji": (-17.75, 177.45, "Pacific/Fiji")}, "hint_calls": []}
    cfg = tmp_path / "config.yml"
    cfg.write_text(f"state_root: {tmp_path / 'state'}\n"
                   "pg: {host: h, port: 1, user: u, password: p, database: d}\n"
                   "immich: {url: http://x, api_key: k, library_id: l}\n")
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: _Conn(db))
    monkeypatch.setattr(cli, "ImmichClient", _Api)
    _Api.refreshed, _Api.down = [], False

    import subprocess
    real_run = subprocess.run

    def fake_run(args, **kw):
        if args and args[0] == "exiftool" and "-@" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps(exif), "")
        return real_run(args, **kw)
    monkeypatch.setattr(subprocess, "run", fake_run)
    writes = []
    from immy import sidecar as sidecar_mod

    def fake_write(media, patch, *, xmp_path=None):
        writes.append((media.name, patch))
        old = sc.read_sidecar(xmp_path.read_text() if xmp_path.exists() else "")
        xmp_path.write_text(_xmp(patch.get("DateTimeOriginal") or old.dto,
                                 patch.get("GPSLatitude", old.lat), patch.get("GPSLongitude", old.lon)))
        return xmp_path
    monkeypatch.setattr(sidecar_mod, "write", fake_write)
    db["exif"], db["root"] = exif, root
    return ["sidecars", "check", "--config", str(cfg), "--originals", str(root)], db, writes, tmp_path


def test_cli_dry_run_reports_and_writes_nothing(lib) -> None:
    args, db, writes, tmp = lib
    out = tmp / "plan.csv"
    res = CliRunner().invoke(cli.app, args + ["--csv", str(out)])
    assert res.exit_code == 0, res.output
    assert "to repair 2 sidecar(s)" in " ".join(res.output.split())
    assert writes == [] and _Api.refreshed == []
    assert db["hint_calls"] == [["fiji"]]                   # only what the file can't settle
    assert "gps-sign+clock-utc" in out.read_text()


def test_cli_apply_repairs_logs_and_refreshes_then_skips_unchanged(lib) -> None:
    args, db, writes, tmp = lib
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert res.exit_code == 0, res.output
    got = dict(writes)
    assert got["IMG_3915.MOV"]["DateTimeOriginal"] == "2023:11:30 15:08:04-08:00"
    assert got["mcp_video-1.MOV"]["GPSLatitude"] == -17.803
    assert got["mcp_video-1.MOV"]["DateTimeOriginal"] == "2025:10:08 03:29:23+12:00"
    assert "IMG_1.MOV" not in got
    assert _Api.refreshed == [["fiji", "vegas"]]
    log = next((tmp / "state").glob("sidecar-check-*.jsonl")).read_text().splitlines()
    assert len(log) == 2 and all(json.loads(line)["before"] for line in log)
    # The next run (the ingest's routine stage) only looks at new or changed ones.
    writes.clear()
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert "3 unchanged" in " ".join(res.output.split()) and writes == []



# --- review cases ----------------------------------------------------------------


def test_insta360_local_clock_is_never_read_as_utc() -> None:
    f = sc.file_facts({"CreateDate": "2025:10:27 13:55:44"}, "VID_20251027_135544_00_001.mp4")
    assert f.utc is None
    assert sc.plan(f, sc.SidecarFacts("2025-10-27T13:55:44"), sc.Hint(zone=timezone(timedelta(hours=-10)))) is None
    by_make = sc.file_facts({"Make": "Insta360", "CreateDate": "2025:10:27 13:55:44"}, "x.mp4")
    assert by_make.utc is None


def test_only_a_lost_minus_far_away_is_a_sign_bug() -> None:
    greenwich = sc.FileFacts(51.48, -0.00002, None, None, False)
    assert sc.plan(greenwich, sc.SidecarFacts(None, 51.48, 0.00002)) is None    # metres: an edit
    east = sc.FileFacts(-17.8, 177.4, None, None, False)
    assert sc.plan(east, sc.SidecarFacts(None, -17.8, -177.4)) is None          # abs() can't add a minus
    equator = sc.FileFacts(utc=datetime(2025, 1, 1, tzinfo=UTC), video=True)
    near = sc.Hint(-0.1, 36.8, None)                                            # Nairobi-ish
    assert sc.plan(equator, sc.SidecarFacts(None, 0.1, 36.8), near) is None     # mirror 22 km away


def test_agrees_checks_every_changed_field_against_the_file() -> None:
    heic = sc.FileFacts(36.1322, -115.1661, datetime.fromisoformat("2023-11-30T15:08:04-08:00"))
    assert sc.agrees(heic, {"DateTimeOriginal": "2023:11:30 15:08:04-08:00", "GPSLatitude": 36.1322,
                            "GPSLongitude": -115.1661})
    assert not sc.agrees(heic, {"DateTimeOriginal": "2023:11:30 16:08:04-08:00"})
    assert not sc.agrees(heic, {"GPSLatitude": 36.1322, "GPSLongitude": 115.1661})
    assert not sc.agrees(sc.FileFacts(read=False), {})


def _share_vegas(db, root, still_exif):
    """A Live Photo still sharing IMG_3915.xmp with the video."""
    (root / "v" / "IMG_3915.HEIC").write_bytes(b"")
    db["sidecars"].append(("still", "/lib/v/IMG_3915.HEIC", "/lib/v/IMG_3915.xmp",
                           datetime(2025, 1, 1, tzinfo=UTC)))
    db["exif"].append({"SourceFile": str(root / "v" / "IMG_3915.HEIC"), **still_exif})


def test_cli_repairs_a_shared_sidecar_only_when_every_asset_agrees(lib) -> None:
    args, db, writes, tmp = lib
    _share_vegas(db, db["root"], {"DateTimeOriginal": "2023:11:30 15:08:04", "OffsetTimeOriginal": "-08:00",
                                  "GPSLatitude": 36.1322, "GPSLongitude": -115.1661})
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert res.exit_code == 0, res.output
    assert "IMG_3915.MOV" in dict(writes)
    assert ["fiji", "still", "vegas"] in _Api.refreshed          # the sibling is refreshed too


def test_cli_leaves_a_shared_sidecar_whose_assets_disagree(lib) -> None:
    args, db, writes, tmp = lib
    # The still says it was taken an hour later: the video's fix would make it wrong.
    _share_vegas(db, db["root"], {"DateTimeOriginal": "2023:11:30 16:08:04", "OffsetTimeOriginal": "-08:00"})
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert "1 shared sidecar(s) whose assets disagree" in " ".join(res.output.split())
    assert "IMG_3915.MOV" not in dict(writes)


def test_cli_logs_before_it_writes(lib, monkeypatch) -> None:
    args, db, writes, tmp = lib
    from immy import sidecar as sidecar_mod

    def boom(media, patch, *, xmp_path=None):
        log = next((tmp / "state").glob("sidecar-check-*.jsonl")).read_text()
        assert xmp_path.name in log                               # already recorded
        raise RuntimeError("disk full")
    monkeypatch.setattr(sidecar_mod, "write", boom)
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert "sidecar failed" in res.output


def test_cli_an_unreadable_original_is_skipped_and_retried(lib) -> None:
    args, db, writes, tmp = lib
    db["exif"][:] = [e for e in db["exif"] if not e["SourceFile"].endswith("IMG_3915.MOV")]
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert "1 with an unreadable original" in " ".join(res.output.split())
    assert "IMG_3915.MOV" not in dict(writes)
    state = json.loads((tmp / "state" / "sidecar-check.json").read_text())
    assert "vegas" not in state["seen"]                           # not stamped: next run retries


def test_cli_a_changed_original_is_checked_again(lib) -> None:
    import os
    args, db, writes, tmp = lib
    CliRunner().invoke(cli.app, args + ["--apply"])
    ok = db["root"] / "v" / "IMG_1.MOV"
    os.utime(ok, ns=(1, 1))                                       # the original was replaced
    res = CliRunner().invoke(cli.app, args)
    assert "1 to check" in " ".join(res.output.split())


def test_cli_a_failed_refresh_is_retried_next_run(lib) -> None:
    args, db, writes, tmp = lib
    _Api.down = True
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert res.exit_code == 1 and "retried on the next run" in res.output
    _Api.down = False
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert res.exit_code == 0, res.output
    assert _Api.refreshed == [["fiji", "vegas"]]



def test_a_still_with_only_a_wall_clock_can_veto() -> None:
    still = sc.file_facts({"DateTimeOriginal": "2023:11:30 10:00:00"}, "IMG_1.HEIC")
    assert still.wall == datetime(2023, 11, 30, 10, 0) and still.local is None
    assert sc.agrees(still, {"DateTimeOriginal": "2023:11:30 10:00:30+02:00"})
    assert not sc.agrees(still, {"DateTimeOriginal": "2023:11:30 12:00:00+02:00"})


def test_cli_skips_a_sidecar_another_user_shares(lib) -> None:
    args, db, writes, tmp = lib
    db["cross"] = {"/lib/v/IMG_3915.xmp"}
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert "1 shared with another user's assets skipped" in " ".join(res.output.split())
    assert "IMG_3915.MOV" not in dict(writes) and "mcp_video-1.MOV" in dict(writes)


def test_cli_refresh_intent_survives_an_interrupted_run(lib, monkeypatch) -> None:
    args, db, writes, tmp = lib
    from immy import sidecar as sidecar_mod
    real = sidecar_mod.write
    calls = []

    def die_on_second(media, patch, *, xmp_path=None):
        calls.append(media.name)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(media, patch, xmp_path=xmp_path)
    monkeypatch.setattr(sidecar_mod, "write", die_on_second)
    CliRunner().invoke(cli.app, args + ["--apply"])
    assert (tmp / "state" / "sidecar-check-pending.txt").read_text().split()
    monkeypatch.setattr(sidecar_mod, "write", real)
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert res.exit_code == 0, res.output
    assert set(_Api.refreshed[-1]) >= {"fiji", "vegas"}
    assert not (tmp / "state" / "sidecar-check-pending.txt").exists()



# --- videos on the UTC clock with no sidecar -------------------------------------


def test_zone_fix_dates_a_utc_clock_video_from_its_neighbours() -> None:
    from zoneinfo import ZoneInfo
    meta = sc.file_facts({"CreationDate": "2026:05:10 04:52:29Z"}, "mcp_video-20787.mov")
    shown = datetime(2026, 5, 10, 4, 52, 29)
    assert sc.zone_fix(meta, shown, sc.Hint(zone=ZoneInfo("Asia/Kolkata"))) == {
        "DateTimeOriginal": "2026:05:10 10:22:29+05:30"}
    assert sc.zone_fix(meta, shown, None) is None                      # no zone: no guess
    assert sc.zone_fix(meta, shown + timedelta(hours=5), sc.Hint(zone=ZoneInfo("Asia/Kolkata"))) is None
    winter = sc.file_facts({"CreationDate": "2026:01:10 04:52:29Z"}, "mcp_video-1.mov")
    assert sc.zone_fix(winter, datetime(2026, 1, 10, 4, 52, 29),
                       sc.Hint(zone=ZoneInfo("Europe/London"))) is None    # UTC is the local clock
    apple = sc.file_facts({"CreateDate": "2023:11:30 23:08:04",
                           "CreationDate": "2023:11:30 15:08:04-08:00"}, "IMG_1.MOV")
    assert sc.zone_fix(apple, datetime(2023, 11, 30, 23, 8, 4), None) == {
        "DateTimeOriginal": "2023:11:30 15:08:04-08:00"}                   # the file's own offset
    insta = sc.file_facts({"CreateDate": "2025:10:27 13:55:44"}, "VID_20251027_135544_00_001.mp4")
    assert sc.zone_fix(insta, datetime(2025, 10, 27, 13, 55, 44), sc.Hint(zone=ZoneInfo("Pacific/Honolulu"))) is None


def test_cli_dates_utc_clock_videos_with_a_sidecar_of_their_own(lib) -> None:
    args, db, writes, tmp = lib
    root = db["root"]
    (root / "v" / "mcp_video-20787.mov").write_bytes(b"")
    db["utc"] = [("mumbai", "/lib/v/mcp_video-20787.mov", datetime(2026, 5, 10, 4, 52, 29),
                  datetime(2026, 5, 10, 4, 52, 29, tzinfo=UTC))]
    db["exif"].append({"SourceFile": str(root / "v" / "mcp_video-20787.mov"),
                       "CreationDate": "2026:05:10 04:52:29Z"})
    db["hints"]["mumbai"] = (None, None, "Asia/Kolkata")
    res = CliRunner().invoke(cli.app, args + ["--apply"])
    assert res.exit_code == 0, res.output
    assert "1 video(s) dated" in " ".join(res.output.split())
    assert dict(writes)["mcp_video-20787.mov"] == {"DateTimeOriginal": "2026:05:10 10:22:29+05:30"}
    assert (root / "v" / "mcp_video-20787.mov.xmp").exists()
    assert db["registered"] == [("mumbai", "/lib/v/mcp_video-20787.mov.xmp")]
    assert "mumbai" in _Api.refreshed[-1]
