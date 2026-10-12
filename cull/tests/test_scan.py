import json

from typer.testing import CliRunner

from cull import scan as scan_mod
from cull.cli import app

SRT = """1
00:00:00,000 --> 00:00:00,033
2022-01-30 12:29:38.078
[latitude: 50.23553] [longitude: 30.46274]
"""


def build(tmp_path, tools):
    src = tmp_path / "Cine"
    tools["clip"](src / "30Jan-Gvo" / "DJI_0054.MP4", codec="mpeg4")       # needs a proxy
    (src / "30Jan-Gvo" / "DJI_0054.SRT").write_text(SRT)
    tools["clip"](src / "30Jan-Gvo" / "DJI_0055.MP4")                       # plays as is
    tools["clip"](src / "30Jan-Gvo" / "DJI_0056.MP4", codec="mpeg4")
    (src / "30Jan-Gvo" / "DJI_0056.LRF").write_bytes((src / "30Jan-Gvo" / "DJI_0055.MP4").read_bytes())
    tools["jpeg"](src / "30Jan-Gvo" / "DJI_0060.JPG")
    (src / "stray.LRF").write_bytes(b"x")
    return src


def test_scan_builds_items(tmp_path, media_tools):
    src = build(tmp_path, media_tools)
    work = tmp_path / "work"
    doc = scan_mod.scan(work, [src], geocode=False, log=lambda s: None)
    by = {i["id"]: i for i in doc["items"]}
    assert set(by) == {"Cine/30Jan-Gvo/DJI_0054.MP4", "Cine/30Jan-Gvo/DJI_0055.MP4",
                       "Cine/30Jan-Gvo/DJI_0056.MP4", "Cine/30Jan-Gvo/DJI_0060.JPG"}
    a = by["Cine/30Jan-Gvo/DJI_0054.MP4"]
    assert (a["time"], a["time_src"], a["lat"]) == ("2022-01-30T12:29:38", "srt", 50.23553)
    assert a["play"].startswith("/work/proxies/") and (work / "_cull" / a["play"][len("/work/"):]).exists()
    assert by["Cine/30Jan-Gvo/DJI_0055.MP4"]["play"] == "/media/Cine/30Jan-Gvo/DJI_0055.MP4"
    assert by["Cine/30Jan-Gvo/DJI_0056.MP4"]["play"] == "/media/Cine/30Jan-Gvo/DJI_0056.LRF"
    photo = by["Cine/30Jan-Gvo/DJI_0060.JPG"]
    assert photo["kind"] == "photo" and photo["image"] == "/media/Cine/30Jan-Gvo/DJI_0060.JPG"
    assert photo["time"] == "2022-01-30T12:31:00" and photo["model"] == "FC4170"
    assert all(i["thumb"] for i in doc["items"])
    assert doc["orphans"] == [{"src": "Cine", "rel": "stray.LRF"}]
    assert json.loads((work / "_cull" / "items.json").read_text())["sources"] == {"Cine": str(src.resolve())}


def test_rescan_uses_cache_and_keeps_sources(tmp_path, media_tools, monkeypatch):
    src = build(tmp_path, media_tools)
    work = tmp_path / "work"
    scan_mod.scan(work, [src], geocode=False, log=lambda s: None)
    calls = []
    monkeypatch.setattr(scan_mod.timeplace, "exif_batch", lambda paths: calls.append(paths) or {})
    doc = scan_mod.scan(work, [], geocode=False, log=lambda s: None)   # no --src: registered sources
    assert len(doc["items"]) == 4 and calls == [[]]


def test_missing_source_warns_and_keeps_decisions(tmp_path, media_tools):
    src = build(tmp_path, media_tools)
    work = tmp_path / "work"
    scan_mod.scan(work, [src], geocode=False, log=lambda s: None)
    (work / "_cull" / "decisions.json").write_text('{"Cine/30Jan-Gvo/DJI_0055.MP4": {"keep": "keep"}}')
    src.rename(tmp_path / "moved")
    logs = []
    doc = scan_mod.scan(work, [], geocode=False, log=logs.append)
    assert doc["items"] == [] and any("missing" in s for s in logs)
    assert "Cine" in doc["sources"]
    assert json.loads((work / "_cull" / "decisions.json").read_text()) == {
        "Cine/30Jan-Gvo/DJI_0055.MP4": {"keep": "keep"}}


def test_unreadable_video_is_flagged(tmp_path, media_tools):
    src = tmp_path / "s"
    src.mkdir()
    (src / "broken.MP4").write_bytes(b"nope")
    doc = scan_mod.scan(tmp_path / "w", [src], geocode=False, log=lambda s: None)
    assert "unreadable" in doc["items"][0]["flags"]


def test_cli_scan(tmp_path, media_tools):
    src = build(tmp_path, media_tools)
    r = CliRunner().invoke(app, ["scan", "--work", str(tmp_path / "w"), "--src", str(src), "--no-geocode"],
                           env={"COLUMNS": "200"})
    assert r.exit_code == 0, r.output
    assert "Cine" in r.output and "orphan" in r.output


def _scan(work, srcs, **kw):
    return scan_mod.scan(work, srcs, geocode=kw.pop("geocode", False), log=kw.pop("log", lambda s: None), **kw)


def test_no_proxies_then_proxies(tmp_path, media_tools):
    src = tmp_path / "s"
    media_tools["clip"](src / "a.MP4", codec="mpeg4")
    work = tmp_path / "w"
    it = _scan(work, [src], proxies=False)["items"][0]
    assert it["play"] is None and "no-proxy" in it["flags"]
    it = _scan(work, [], proxies=True)["items"][0]
    assert it["play"].startswith("/work/proxies/") and "no-proxy" not in it["flags"]


def test_time_from_mtime_flag(tmp_path, media_tools):
    src = tmp_path / "s"
    media_tools["jpeg"](src / "p.JPG", dto=None)
    it = _scan(tmp_path / "w", [src])["items"][0]
    assert it["time_src"] == "mtime" and "time-from-mtime" in it["flags"]


class FakeImmich:
    def __init__(self, hit=None, boom=False):
        self.hit, self.boom = hit, boom

    def candidates(self, item):
        if self.boom:
            raise RuntimeError("db gone")
        return ["/x"] if item["id"] == self.hit else []


def test_immich_hints_requeried_and_failure_tolerated(tmp_path, media_tools):
    src = build(tmp_path, media_tools)
    work = tmp_path / "w"
    hit = "Cine/30Jan-Gvo/DJI_0055.MP4"
    doc = _scan(work, [src], immich=FakeImmich(hit))
    assert {i["id"]: i["immich"] for i in doc["items"]}[hit] == ["/x"]
    doc = _scan(work, [], immich=FakeImmich(None))
    assert all(i["immich"] == [] for i in doc["items"])
    logs = []
    doc = _scan(work, [], immich=FakeImmich(boom=True), log=logs.append)
    assert all(i["immich"] == [] for i in doc["items"]) and len(doc["items"]) == 4
    assert sum("Immich check failed" in s for s in logs) == 1
    assert json.loads((work / "_cull" / "items.json").read_text())["items"]


def test_source_names_are_stable(tmp_path, media_tools):
    a, b = tmp_path / "a" / "Cine", tmp_path / "b" / "Cine"
    media_tools["jpeg"](a / "1.JPG")
    media_tools["jpeg"](b / "2.JPG")
    work = tmp_path / "w"
    ids = {i["id"] for i in _scan(work, [a, b])["items"]}
    assert ids == {"Cine/1.JPG", "Cine-2/2.JPG"}
    assert {i["id"] for i in _scan(work, [b, a])["items"]} == ids


def test_geocode_uses_cache(tmp_path, media_tools):
    src = build(tmp_path, media_tools)
    work = tmp_path / "w"
    (work / "_cull").mkdir(parents=True)
    (work / "_cull" / "geocache.json").write_text(json.dumps({"50.236,30.463": "Kyiv"}))
    doc = _scan(work, [src], geocode=True)
    assert {i["id"]: i["place"] for i in doc["items"]}["Cine/30Jan-Gvo/DJI_0054.MP4"] == "Kyiv"
