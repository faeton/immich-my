"""`immy takeout redate`: linking, dating, zoning, twins. Synthetic only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from immy import takeout_redate as tr

PREFIX = "/staging/google-takeout"
UTC = timezone.utc


def _json(folder: Path, name: str, title: str, when: datetime, geo=None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = {"title": title, "photoTakenTime": {"timestamp": str(int(when.timestamp()))}}
    if geo:
        body["geoData"] = {"latitude": geo[0], "longitude": geo[1]}
    (folder / name).write_text(json.dumps(body))


def _target(rel: str, local: datetime, reason="placeholder", gps=(None, None), aid="a1"):
    return tr.Target(aid, rel, local, gps[0], gps[1], reason)


def _plan(tmp_path, targets, index, zone=None, library_root=None):
    return tr.plan(targets, index, takeout_root=tmp_path / "takeout",
                   staging_prefix=PREFIX, neighbour_zone=lambda *_: zone,
                   library_root=library_root)


FOLDER_2019 = "unpacked/Takeout/Google Photos/Photos from 2019"
STAGED = f"{PREFIX}/{FOLDER_2019}/IMG_1711(1).MP4"
SHOT = datetime(2019, 11, 15, 1, 43, 25, tzinfo=UTC)   # evening of the 14th in Chicago
PLACEHOLDER = datetime(2019, 1, 1, 12, 0, 0)


def test_manifest_index_covers_promote_dates_and_collision_names() -> None:
    rows = [(42, STAGED, "2026-06-20T18:10:05", None),
            (43, f"{PREFIX}/x/IMG_9.JPG", "2026-06-21T00:00:00", "2019-03-01T10:00:00")]
    idx = tr.manifest_index(rows)
    assert idx["2026/06/IMG_1711(1).MP4"] == {STAGED}
    assert idx["2026/06/IMG_1711(1)__42.MP4"] == {STAGED}
    # Promoted under its pre-fix date, so both folders are candidates.
    assert f"{PREFIX}/x/IMG_9.JPG" in idx["2019/03/IMG_9.JPG"]
    assert f"{PREFIX}/x/IMG_9.JPG" in idx["2026/06/IMG_9.JPG"]


def test_placeholder_gets_jsons_instant_on_the_local_clock(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER, gps=(41.88, -87.63))],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert fix.problem is None
    assert fix.zone_source == "file gps"
    assert fix.xmp == "2019:11:14 19:43:25-06:00"


def test_zone_falls_back_to_json_gps_then_nearby_shots_then_utc(tmp_path) -> None:
    folder = tmp_path / "takeout" / FOLDER_2019
    _json(folder, "IMG_1.JPG.supplemental-metadata(1).json", "IMG_1.JPG", SHOT, geo=(41.88, -87.63))
    _json(folder, "IMG_2.JPG.supplemental-metadata(1).json", "IMG_2.JPG", SHOT)
    idx = {f"2026/06/IMG_{i}(1).JPG": {f"{PREFIX}/{FOLDER_2019}/IMG_{i}(1).JPG"} for i in (1, 2)}
    t1 = _target("2026/06/IMG_1(1).JPG", PLACEHOLDER, aid="1")
    t2 = _target("2026/06/IMG_2(1).JPG", PLACEHOLDER, aid="2")
    f1, f2 = _plan(tmp_path, [t1, t2], idx, zone="UTC+2")
    assert (f1.zone_source, f1.xmp) == ("json gps", "2019:11:14 19:43:25-06:00")
    assert (f2.zone_source, f2.xmp) == ("nearby shots", "2019:11:15 03:43:25+02:00")
    _, f2 = _plan(tmp_path, [t1, t2], idx, zone=None)
    assert (f2.zone_source, f2.xmp) == ("utc", "2019:11:15 01:43:25+00:00")


def test_placeholder_source_must_sit_in_its_year_folder(tmp_path) -> None:
    other = f"{PREFIX}/unpacked/Takeout/Google Photos/Photos from 2023/IMG_1711(1).MP4"
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    _json(tmp_path / "takeout/unpacked/Takeout/Google Photos/Photos from 2023",
          "IMG_1711.HEIC.supplemental-metadata(1).json", "IMG_1711.HEIC",
          datetime(2023, 5, 1, tzinfo=UTC))
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER)],
                 {"2026/06/IMG_1711(1).MP4": {STAGED, other}})
    assert fix.problem is None
    assert fix.staging == STAGED


def test_utc_asset_is_rezoned_only_if_it_shows_googles_instant(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT, geo=(41.88, -87.63))
    idx = {"2026/06/IMG_1711(1).MP4": {STAGED}}
    shows_utc = SHOT.replace(tzinfo=None)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", shows_utc, reason="utc")], idx)
    assert fix.problem is None and fix.xmp == "2019:11:14 19:43:25-06:00"
    # Its own (camera) date disagrees with Google's: hands off.
    own = shows_utc + timedelta(hours=7, minutes=3)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", own, reason="utc")], idx)
    assert fix.problem == "own date disagrees with takeout"


def test_utc_without_any_zone_is_left_alone(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", SHOT.replace(tzinfo=None), reason="utc")],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert fix.problem == "still no zone"


def test_agreeing_duplicate_sources_are_one_answer(tmp_path) -> None:
    other_folder = "unpacked/Takeout/Google Photos/Archive"
    for f in (FOLDER_2019, other_folder):
        _json(tmp_path / "takeout" / f, "IMG_1711.HEIC.supplemental-metadata(1).json",
              "IMG_1711.HEIC", SHOT)
    idx = {"2026/06/IMG_1711(1).MP4": {STAGED, f"{PREFIX}/{other_folder}/IMG_1711(1).MP4"}}
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER)], idx)
    assert fix.problem is None


def test_no_source_and_no_json(tmp_path) -> None:
    a, b = _plan(tmp_path, [_target("2026/06/X.JPG", PLACEHOLDER, aid="x"),
                            _target("2026/06/IMG_1711(1).MP4", PLACEHOLDER, aid="y")],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert a.problem == "no takeout source"
    assert b.problem == "no json, no datable neighbours"


def test_neighbours_need_both_sides_and_agreement(tmp_path) -> None:
    folder = tmp_path / "takeout" / FOLDER_2019
    t0 = datetime(2018, 5, 7, 10, 0, tzinfo=UTC)
    _json(folder, "IMG_0210.mp4.supplemental-metadata.json", "IMG_0210.mp4", t0)
    _json(folder, "IMG_0214.mp4.supplemental-metadata.json", "IMG_0214.mp4", t0 + timedelta(hours=4))
    got = tr.neighbour_taken(folder / "IMG_0212(1).mp4")
    assert got and got.instant == t0 + timedelta(hours=2) and got.source == "neighbour-file"
    # Another device's .HEIC with that number doesn't count.
    assert tr.neighbour_taken(folder / "IMG_0212(1).HEIC") is None
    # Neighbours a week apart: not one session, no answer.
    _json(folder, "IMG_0310.mp4.supplemental-metadata.json", "IMG_0310.mp4", t0)
    _json(folder, "IMG_0312.mp4.supplemental-metadata.json", "IMG_0312.mp4", t0 + timedelta(days=7))
    assert tr.neighbour_taken(folder / "IMG_0311(1).mp4") is None


@pytest.mark.parametrize("name,expect", [
    ("Europe/Lisbon", "Europe/Lisbon"), ("UTC+2", "UTC+02:00"), ("UTC-03:30", "UTC-03:30"),
    ("UTC", None), ("UTC+0", None), ("nonsense", None), (None, None),
])
def test_parse_zone(name, expect) -> None:
    z = tr.parse_zone(name)
    assert (tr.zone_label(z) if z else None) == expect


def test_original_name() -> None:
    assert tr.original_name("IMG_1711(1).MP4") == "IMG_1711.MP4"
    assert tr.original_name("IMG_0076(1)__66958.MP4") == "IMG_0076.MP4"
    assert tr.original_name("IMG_0480__172431.HEIC") == "IMG_0480.HEIC"
    assert tr.original_name("IMG_1.JPG") == "IMG_1.JPG"


def test_twins() -> None:
    def c(aid, delta, clip=None):
        return tr.Candidate(aid, SHOT + delta, None, clip)
    assert tr.is_twin(SHOT, c("x", timedelta(seconds=1)))
    assert tr.is_twin(SHOT, c("x", timedelta(hours=-6)))          # original zoned wrongly
    assert not tr.is_twin(SHOT, c("x", timedelta(hours=6, seconds=30)))
    assert not tr.is_twin(SHOT, c("x", timedelta(days=2)))
    assert not tr.is_twin(SHOT, c("x", timedelta(0), clip=0.4))   # same name, other photo
    assert tr.pick_twin(SHOT, [c("a", timedelta(0)), c("b", timedelta(days=400))]).asset_id == "a"
    assert tr.pick_twin(SHOT, [c("a", timedelta(0)), c("b", timedelta(seconds=1))]) is None


def test_neighbour_needs_the_exact_files_json(tmp_path) -> None:
    """A HEIC neighbour's JSON (another device, or the Live Photo still)
    must not date an .mp4."""
    folder = tmp_path / "takeout" / FOLDER_2019
    t0 = datetime(2018, 5, 7, 10, 0, tzinfo=UTC)
    _json(folder, "IMG_0210.HEIC.supplemental-metadata.json", "IMG_0210.HEIC", t0)
    _json(folder, "IMG_0214.HEIC.supplemental-metadata.json", "IMG_0214.HEIC", t0 + timedelta(hours=4))
    assert tr.neighbour_taken(folder / "IMG_0212(1).mp4") is None


def test_null_island_file_gps_falls_through_to_json_gps(tmp_path) -> None:
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT, geo=(41.88, -87.63))
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER, gps=(0.0, 0.0))],
                 {"2026/06/IMG_1711(1).MP4": {STAGED}})
    assert fix.zone_source == "json gps"
    assert fix.xmp == "2019:11:14 19:43:25-06:00"


def test_placeholder_outside_any_year_folder_is_accepted(tmp_path) -> None:
    archive = "unpacked/Takeout/Google Photos/Archive"
    staged = f"{PREFIX}/{archive}/IMG_1711(1).MP4"
    _json(tmp_path / "takeout" / archive, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT)
    fix, = _plan(tmp_path, [_target("2026/06/IMG_1711(1).MP4", PLACEHOLDER)],
                 {"2026/06/IMG_1711(1).MP4": {staged}})
    assert fix.problem is None


# --- CLI apply: roots, registration, stacking (fakes only) -------------------

import sqlite3

from typer.testing import CliRunner

from immy import cli


class _Cur:
    def __init__(self, db):
        self.db, self.rows = db, []

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        d = self.db
        if 'FROM "user"' in q:
            self.rows = [("u1", "me@example.com")]
        elif "FROM library" in q:
            self.rows = [(d["roots"],)]
        elif "FROM placeholder" in q:
            self.rows = [(aid, path, PLACEHOLDER, None, None, "placeholder")
                         for aid, path in d["placeholders"]]
        elif '"timeZone" IN' in q and "count(*)" not in q:
            self.rows = []
        elif "count(*)" in q and "timeZone" in q:
            self.rows = []
        elif "smart_search" in q:
            self.rows = d["twins"].get(params["orig"], [])
        elif "FROM asset_file" in q:
            self.rows = list(d["registered"].items())
        elif q.startswith("INSERT INTO asset_file"):
            d["inserted"].append(params)
            self.rows = []
        else:
            raise AssertionError(q)
        return self

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _PG:
    def __init__(self, db):
        self.c = _Cur(db)

    def cursor(self):
        return self.c

    def commit(self):
        pass

    def close(self):
        pass


class _Api:
    calls: list = []

    def __init__(self, **kw):
        pass

    def refresh_metadata(self, ids):
        self.calls.append(("refresh", tuple(ids)))

    def create_stack(self, primary, others):
        self.calls.append(("stack", primary, tuple(others)))
        return "s1"


def _setup_cli(tmp_path, monkeypatch, roots, placeholders, registered=None, twins=None):
    manifest = tmp_path / "manifest.sqlite"
    con = sqlite3.connect(manifest)
    con.execute("CREATE TABLE asset (id INTEGER, path TEXT, taken_at TEXT, source TEXT, status TEXT)")
    con.execute("INSERT INTO asset VALUES (7, ?, '2026-06-20T18:10:05', 'google', 'promoted')", (STAGED,))
    con.commit()
    con.close()
    _json(tmp_path / "takeout" / FOLDER_2019, "IMG_1711.HEIC.supplemental-metadata(1).json",
          "IMG_1711.HEIC", SHOT, geo=(41.88, -87.63))
    cfg = tmp_path / "config.yml"
    cfg.write_text(f"state_root: {tmp_path / 'state'}\n"
                   "pg: {host: h, port: 1, user: u, password: p, database: d}\n"
                   "immich: {url: http://x, api_key: k, library_id: l}\n")
    db = {"roots": roots, "placeholders": placeholders, "registered": registered or {},
          "twins": twins or {}, "inserted": []}
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _c: _PG(db))
    monkeypatch.setattr(cli, "ImmichClient", _Api)
    _Api.calls = []
    writes = []
    import immy.sidecar as sc
    monkeypatch.setattr(sc, "write", lambda media, patch, xmp_path=None: writes.append((media, patch, xmp_path)))
    args = ["takeout", "redate", "--config", str(cfg), "--manifest", str(manifest),
            "--takeout-root", str(tmp_path / "takeout"), "--originals", str(tmp_path / "lib"),
            "--no-utc"]
    return db, writes, args


def test_cli_needs_import_path_with_several_roots(tmp_path, monkeypatch) -> None:
    _, _, args = _setup_cli(tmp_path, monkeypatch, ["/a", "/b"], [])
    res = CliRunner().invoke(cli.app, args)
    assert res.exit_code == 2 and "--import-path" in " ".join(res.output.split())


def test_cli_apply_registers_the_sidecar_under_the_chosen_root(tmp_path, monkeypatch) -> None:
    ph = [("a1", "/b/2026/06/IMG_1711(1).MP4"),     # under the chosen root
          ("a2", "/a/2026/06/IMG_1711(1).MP4")]     # under the other root: skipped
    twin_row = ("orig1", datetime(2019, 11, 15, 1, 43, 25, tzinfo=UTC), None, 0.01)
    db, writes, args = _setup_cli(tmp_path, monkeypatch, ["/a", "/b"], ph,
                                  twins={"IMG_1711.MP4": [twin_row]})
    res = CliRunner().invoke(cli.app, args + ["--import-path", "/b", "--apply"])
    assert res.exit_code == 0, res.output
    (media, patch, xmp), = writes
    assert media == tmp_path / "lib/2026/06/IMG_1711(1).MP4"
    assert xmp == tmp_path / "lib/2026/06/IMG_1711(1).MP4.xmp"
    assert patch == {"DateTimeOriginal": "2019:11:14 19:43:25-06:00"}
    (reg,) = db["inserted"]
    assert reg == ("a1", "/b/2026/06/IMG_1711(1).MP4.xmp")
    assert ("refresh", ("a1",)) in _Api.calls
    assert ("stack", "orig1", ("a1",)) in _Api.calls
    log = next((tmp_path / "state").glob("takeout-redate-*.jsonl")).read_text()
    assert '"registered_now": "/b/2026/06/IMG_1711(1).MP4.xmp"' in log


def test_cli_keeps_an_already_registered_sidecar(tmp_path, monkeypatch) -> None:
    db, writes, args = _setup_cli(
        tmp_path, monkeypatch, ["/b"], [("a1", "/b/2026/06/IMG_1711(1).MP4")],
        registered={"a1": "/b/2026/06/IMG_1711(1).xmp"})
    res = CliRunner().invoke(cli.app, args + ["--apply", "--no-stack-copies"])
    assert res.exit_code == 0, res.output
    assert writes[0][2] == tmp_path / "lib/2026/06/IMG_1711(1).xmp"
    assert db["inserted"][0][1] == "/b/2026/06/IMG_1711(1).xmp"
