import json
import os

from typer.testing import CliRunner

from cull import decisions as dec
from cull import stage as st
from cull.cli import app
from cull.core.jsonio import write_json_atomic


def setup(tmp_path, n=2):
    src = tmp_path / "media" / "Cine"
    for i in range(n):
        (src / "30Jan").mkdir(parents=True, exist_ok=True)
        (src / "30Jan" / f"DJI_000{i}.MP4").write_bytes(b"v" * (i + 1))
        (src / "30Jan" / f"DJI_000{i}.SRT").write_text("srt")
    items = [{"id": f"Cine/30Jan/DJI_000{i}.MP4", "src": "Cine", "kind": "video", "flags": [],
              "files": [f"30Jan/DJI_000{i}.MP4", f"30Jan/DJI_000{i}.SRT"], "fp": f"fp{i}"} for i in range(n)]
    work = tmp_path / "media" / "work"
    write_json_atomic(work / "_cull" / "items.json",
                      {"schema": 1, "sources": {"Cine": str(src)}, "items": items, "orphans": []})
    trips = tmp_path / "media" / "trips"
    trips.mkdir()
    return work, trips, src, {i["id"]: i for i in items}


def keep(work, items, ids, trip):
    dec.apply(work, items, ids, {"keep": "keep", "trip": trip}, set())


def test_plan_changes_nothing_and_execute_links(tmp_path):
    work, trips, src, items = setup(tmp_path)
    keep(work, items, list(items), "2022-01-kyiv")
    p = st.plan(work, trips)
    assert [t.trip for t in p.trips] == ["2022-01-kyiv"] and p.trips[0].ok and not list(trips.iterdir())
    assert st.execute(p, trips) == 4
    dst = trips / "2022-01-kyiv" / "Cine" / "30Jan" / "DJI_0000.MP4"
    assert os.path.samefile(dst, src / "30Jan" / "DJI_0000.MP4")
    receipt = json.loads((trips / "2022-01-kyiv" / st.RECEIPT).read_text())
    assert set(receipt["items"]) == set(items) and receipt["items"]["Cine/30Jan/DJI_0000.MP4"]["fp"] == "fp0"


def test_rerun_is_a_noop(tmp_path):
    work, trips, _, items = setup(tmp_path)
    keep(work, items, list(items), "2022-01-kyiv")
    st.execute(st.plan(work, trips), trips)
    rpath = trips / "2022-01-kyiv" / st.RECEIPT
    before = rpath.read_text()
    p = st.plan(work, trips)
    assert all(not ip.links and len(ip.skips) == 2 for ip in p.trips[0].items)
    assert st.execute(p, trips) == 0 and rpath.read_text() == before


def test_kept_without_trip_and_changed_and_conflicts(tmp_path):
    work, trips, _, items = setup(tmp_path, n=3)
    ids = list(items)
    keep(work, items, [ids[0]], "2022-01-kyiv")
    dec.apply(work, items, [ids[1]], {"keep": "keep"}, set())               # no trip
    keep(work, items, [ids[2]], "2022-01-other")
    p = st.plan(work, trips)
    assert p.no_trip == [ids[1]]
    # a different file already sits at a destination of the "other" trip
    clash = trips / "2022-01-other" / "Cine" / "30Jan" / "DJI_0002.SRT"
    clash.parent.mkdir(parents=True)
    clash.write_text("someone else's")
    p = st.plan(work, trips)
    other = next(t for t in p.trips if t.trip == "2022-01-other")
    assert not other.ok and "different file" in other.problems[0]
    assert st.execute(p, trips) == 2                                        # kyiv still stages
    assert not (trips / "2022-01-other" / "Cine" / "30Jan" / "DJI_0002.MP4").exists()
    # changed: the file's fingerprint moved after review
    items2 = {**items, ids[0]: {**items[ids[0]], "fp": "new"}}
    doc = json.loads((work / "_cull" / "items.json").read_text())
    doc["items"] = list(items2.values())
    write_json_atomic(work / "_cull" / "items.json", doc)
    kyiv = next(t for t in st.plan(work, trips).trips if t.trip == "2022-01-kyiv")
    assert any("changed" in x for x in kyiv.problems)


def test_other_filesystem_refused(tmp_path, monkeypatch):
    work, trips, _, items = setup(tmp_path)
    keep(work, items, list(items), "2022-01-kyiv")
    real = os.stat

    class FakeStat:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, k):
            return 999 if k == "st_dev" else getattr(self._s, k)

    def fake_stat(self, *a, **kw):
        s = real(self, *a, **kw)
        return FakeStat(s) if "Cine" in str(self) and "trips" not in str(self) else s

    monkeypatch.setattr(st.Path, "stat", fake_stat)
    p = st.plan(work, trips)
    assert not p.trips[0].ok and "another filesystem" in p.trips[0].problems[0]


def test_link_failure_mid_item_is_undone(tmp_path, monkeypatch):
    work, trips, _, items = setup(tmp_path)
    keep(work, items, list(items), "2022-01-kyiv")
    p = st.plan(work, trips)
    real_link = os.link
    calls = []

    def flaky(a, b, **kw):
        calls.append(b)
        if len(calls) == 4:
            raise OSError(18, "Invalid cross-device link")
        return real_link(a, b, **kw)

    monkeypatch.setattr(st.os, "link", flaky)
    assert st.execute(p, trips) == 2
    receipt = json.loads((trips / "2022-01-kyiv" / st.RECEIPT).read_text())
    assert list(receipt["items"]) == ["Cine/30Jan/DJI_0000.MP4"]
    assert not (trips / "2022-01-kyiv" / "Cine" / "30Jan" / "DJI_0001.MP4").exists()
    assert any("cross-device" in x for x in p.trips[0].problems)


def test_stale_after_moving_a_staged_item(tmp_path):
    work, trips, _, items = setup(tmp_path)
    ids = list(items)
    keep(work, items, ids, "2022-01-kyiv")
    st.execute(st.plan(work, trips), trips)
    dec.apply(work, items, [ids[0]], {"trip": "2022-01-lviv"}, set())
    p = st.plan(work, trips)
    kyiv = next(t for t in p.trips if t.trip == "2022-01-kyiv")
    lviv = next(t for t in p.trips if t.trip == "2022-01-lviv")
    assert kyiv.stale and not kyiv.ok and not lviv.ok and "still staged" in lviv.problems[0]
    # removing the links by hand clears it
    for f in (trips / "2022-01-kyiv" / "Cine" / "30Jan").glob("DJI_0000.*"):
        f.unlink()
    p = st.plan(work, trips)
    assert all(t.ok for t in p.trips)


def test_immich_path_check(tmp_path):
    work, trips, _, items = setup(tmp_path)
    keep(work, items, list(items), "2022-01-kyiv")
    taken = "/mnt/external/originals/2022-01-kyiv/Cine/30Jan/DJI_0001.MP4"
    seen = []
    p = st.plan(work, trips, existing_paths=lambda paths: seen.extend(paths) or {taken})
    assert taken in seen and not p.trips[0].ok and "already an Immich asset" in p.trips[0].problems[0]


def test_cli_stage_and_report(tmp_path):
    work, trips, src, items = setup(tmp_path)
    ids = list(items)
    keep(work, items, [ids[0]], "2022-01-kyiv")
    dec.apply(work, items, [ids[1]], {"keep": "drop"}, set())
    r = CliRunner().invoke(app, ["stage", "--work", str(work), "--trips", str(trips)], env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    assert "--yes" in r.output and not list(trips.iterdir())
    r = CliRunner().invoke(app, ["stage", "--work", str(work), "--trips", str(trips), "--yes"])
    assert r.exit_code == 0 and (trips / "2022-01-kyiv" / "Cine" / "30Jan" / "DJI_0000.MP4").exists()
    r = CliRunner().invoke(app, ["report", "--work", str(work), "--dropped"])
    assert r.exit_code == 0
    assert str(src / "30Jan" / "DJI_0001.MP4") in r.stdout and str(src / "30Jan" / "DJI_0001.SRT") in r.stdout


def test_cli_stage_exit_code_when_something_is_not_staged(tmp_path):
    work, trips, _, items = setup(tmp_path)
    dec.apply(work, items, list(items), {"keep": "keep"}, set())
    r = CliRunner().invoke(app, ["stage", "--work", str(work), "--trips", str(trips), "--yes"])
    assert r.exit_code == 1 and "without a trip" in r.output
