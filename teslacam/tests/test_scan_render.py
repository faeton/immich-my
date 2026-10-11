import json
import shutil
import subprocess
from pathlib import Path

import pytest

from teslacam import decisions as dec
from teslacam import render as render_mod
from teslacam import scan as scan_mod


def _scan(dumps: Path, **kw) -> dict:
    return scan_mod.scan(dumps, dumps / "_review", thumbs=False, **kw)


def test_scan_discovers_layouts_drives_and_duplicates(dumps):
    doc = _scan(dumps, exclude=())
    by_id = {e["id"]: e for e in doc["events"]}
    saved = by_id["Dump1|SavedClips|2026-09-09_19-08-30"]
    assert saved["kind"] == "saved" and saved["cams"] == ["front", "left_repeater", "right_repeater", "back"]
    assert len(saved["segs"]) == 3 and saved["segs"][1]["start"] == pytest.approx(2.0, abs=0.2)
    assert saved["gps_source"] == "json" and saved["city"] == "Granada" and saved["reason_slug"] == "honk"
    # trigger 19:08:27 − first segment 19:06:19 = 128 s, clipped to total (≈6 s)
    assert saved["trigger_s"] == pytest.approx(saved["total"], abs=0.01)
    assert saved["offset"] == "+02:00" and saved["offset_source"] == "gps" and saved["tz"] == "Europe/Madrid"
    sentry = by_id["Dump1|SentryClips|2026-09-09_22-00-00"]
    assert sentry["kind"] == "sentry" and sentry["reason_slug"] == "sentry"
    # no GPS of its own → borrowed from the Saved event 2 h earlier (prev-drive)
    assert sentry["gps_source"] == "prev-drive" and sentry["offset_source"] == "gps"
    flat = by_id["Dump2|2024-05-14_14-34-55__prien-am-chiemsee__honk"]
    assert flat["kind"] == "saved" and flat["city"] == "Prien am Chiemsee" and flat["offset"] == "+02:00"
    drives = [e for e in doc["events"] if e["kind"] == "recent"]
    assert len(drives) == 2 and all(d["trigger_s"] is None for d in drives)
    assert drives[0]["id"] == "Dump1|RecentClips|2025-08-01_16-00-00"       # newest first
    assert len(drives[1]["segs"]) == 2
    # the folder clock minus UTC → +01:00 for the UK drive (no GPS)
    assert drives[0]["offset"] == "+01:00" and drives[0]["offset_source"] == "folder"
    assert doc["duplicates"] == [{"id": "Dump3|SavedClips|2026-09-09_19-08-30",
                                  "duplicate_of": ["Dump1|SavedClips|2026-09-09_19-08-30"]}]
    assert (dumps / "_review" / "events.json").exists()
    assert (dumps / "_review" / "probe-cache.json").exists()


def test_scan_exclude(dumps):
    doc = _scan(dumps, exclude=("Dump2", "Dump3"))
    assert not any(e["dump"] in ("Dump2", "Dump3") for e in doc["events"])


def test_scan_thumbs(dumps):
    scan_mod.scan(dumps, dumps / "_review", thumbs=True, exclude=("Dump3",))
    thumbs = list((dumps / "_review" / "thumbs").glob("*.jpg"))
    assert len(thumbs) >= 4 and all(t.stat().st_size > 0 for t in thumbs)


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_render_pilot(dumps):
    _scan(dumps, exclude=("Dump3",))
    rd = dumps / "_review"
    eid = "Dump1|SavedClips|2026-09-09_19-08-30"
    dec.update(rd, eid, {"keep": "keep", "cameras": ["front", "back"], "in_s": 1.0, "out_s": 5.0,
                         "tags": ["crash"], "note": "pilot"})
    root, events = scan_mod.load_events(rd)
    jobs = render_mod.plan(events, dec.load_all(rd), dumps / "render")
    assert [j.ev.id for j in jobs] == [eid]
    job = jobs[0]
    assert (job.a, job.b) == (1.0, 5.0) and job.cams == ["front", "back"]
    assert job.folder.name == "2026-tesla-dashcam" and job.slug == "saved-2026-09-09_19-08-30"
    rec = render_mod.render_job(root, job, model="Model 3", log=lambda s: None)
    out = job.out_dir
    assert (out / "mosaic.mp4").exists() and (out / "front.mp4").exists() and (out / "back.mp4").exists()
    assert not (out / "left_repeater.mp4").exists()
    m = _probe(out / "mosaic.mp4")
    assert float(m["format"]["duration"]) == pytest.approx(4.0, abs=0.6)
    vids = [s for s in m["streams"] if s["codec_type"] == "video"]
    assert vids[0]["codec_name"] == "h264" and vids[0]["width"] == 960 and vids[0]["height"] == 720   # 2×2 of 480×360
    f = _probe(out / "front.mp4")
    assert {s["codec_type"] for s in f["streams"]} == {"video", "subtitle"}
    # lossless cut follows the window at keyframe granularity (fixture: a keyframe
    # every 0.5 s, so 1.0 is exact): 1.0–5.0 spans all three 2 s segments → 4 s
    assert float(f["format"]["duration"]) == pytest.approx(4.0, abs=0.6)
    assert rec["covered"]["front"] == [pytest.approx(1.0, abs=0.01), pytest.approx(5.0, abs=0.3)]
    assert [s for s in f["streams"] if s["codec_type"] == "video"][0]["codec_name"] == "h264"
    # baked metadata
    tags = json.loads(subprocess.run(["exiftool", "-j", "-n", "-Keys:CreationDate", "-Keys:GPSCoordinates", "-Keys:Make",
                                      "-Keys:Model", "-Keys:Description", str(out / "mosaic.mp4")],
                                     capture_output=True, text=True).stdout)[0]
    assert tags["CreationDate"].startswith("2026:09:09 19:06:20+02:00")
    assert tags["Make"] == "Tesla" and tags["Model"] == "Model 3"
    assert "37.17" in tags["GPSCoordinates"] and "#crash" in tags["Description"] and "pilot" in tags["Description"]
    child_tags = json.loads(subprocess.run(["exiftool", "-j", "-n", "-Keys:GPSCoordinates", "-Keys:CreationDate",
                                            str(out / "front.mp4")], capture_output=True, text=True).stdout)[0]
    # children carry the same date AND the same GPS: immy's audit treats a
    # video without GPS as a HIGH finding, and the owner wants every file placed
    assert child_tags["GPSCoordinates"] == tags["GPSCoordinates"]
    assert child_tags["CreationDate"] == tags["CreationDate"]
    assert rec["stamp"] == render_mod.STAMP_VERSION
    # manifest + idempotency
    man = json.loads(render_mod.write_manifest(job.folder, {eid}, album="Tesla Dashcam").read_text())
    g = man["groups"][0]
    assert g["primary"].endswith("/mosaic.mp4") and len(g["children"]) == 2
    assert g["tags"] == ["Source/Tesla", "Gear/Camera/Tesla Model 3", "Tesla/Saved", "Tesla/Reason/honk", "Tesla/crash"]
    assert render_mod.is_done(job)
    # change a decision → new digest → re-render needed; camera dropped → stale file removed
    dec.update(rd, eid, {"cameras": ["front"]})
    jobs2 = render_mod.plan(events, dec.load_all(rd), dumps / "render")
    assert not render_mod.is_done(jobs2[0])
    render_mod.render_job(root, jobs2[0], model="Model 3", log=lambda s: None)
    assert not (out / "back.mp4").exists() and (out / "front.mp4").exists()


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_restamp_changes_model_without_reencode(dumps):
    _scan(dumps, exclude=("Dump3",))
    rd = dumps / "_review"
    eid = "Dump1|SavedClips|2026-09-09_19-08-30"
    dec.update(rd, eid, {"keep": "keep", "cameras": ["front"], "in_s": 1.0, "out_s": 3.0})
    root, events = scan_mod.load_events(rd)
    job = render_mod.plan(events, dec.load_all(rd), dumps / "render")[0]
    rec = render_mod.render_job(root, job, model="Model 3", log=lambda s: None)
    assert rec["model"] == "Model 3" and render_mod.is_done(job, "Model 3") and not render_mod.is_done(job, "Model X")
    first_render = rec["rendered_at"]
    probe_before = _probe(job.out_dir / "mosaic.mp4")["format"]["duration"]
    rec2 = render_mod.render_job(root, job, model="Model X", log=lambda s: None)
    assert rec2["rendered_at"] == first_render and "restamped_at" in rec2
    assert rec2["tags"][1] == "Gear/Camera/Tesla Model X" and render_mod.is_done(job, "Model X")
    assert _probe(job.out_dir / "mosaic.mp4")["format"]["duration"] == probe_before
    for name in ("mosaic.mp4", "front.mp4"):
        t = json.loads(subprocess.run(["exiftool", "-j", "-Keys:Model", "-Keys:Make", str(job.out_dir / name)],
                                      capture_output=True, text=True).stdout)[0]
        assert t["Model"] == "Model X" and t["Make"] == "Tesla"
    # unknown car: plain Tesla — no Model written, gear tag without a model
    rec3 = render_mod.render_job(root, job, model="", log=lambda s: None)
    assert rec3["tags"][1] == "Gear/Camera/Tesla"
    t = json.loads(subprocess.run(["exiftool", "-j", "-Keys:Model", str(job.out_dir / "mosaic.mp4")],
                                  capture_output=True, text=True).stdout)[0]
    assert "Model" not in t


def test_camera_without_footage_in_window_is_skipped(dumps):
    (dumps / "Dump1" / "SavedClips" / "2026-09-09_19-08-30" / "2026-09-09_19-06-19-back.mp4").unlink()
    _scan(dumps, exclude=("Dump3",))
    rd = dumps / "_review"
    eid = "Dump1|SavedClips|2026-09-09_19-08-30"
    dec.update(rd, eid, {"keep": "keep", "cameras": ["front", "back"], "in_s": 0.2, "out_s": 1.6})
    root, events = scan_mod.load_events(rd)
    job = render_mod.plan(events, dec.load_all(rd), dumps / "render")[0]
    logs: list[str] = []
    rec = render_mod.render_job(root, job, model="", log=logs.append)
    assert rec["children"] == [f"{job.slug}/front.mp4"] and not (job.out_dir / "back.mp4").exists()
    assert any("skipped: back" in s for s in logs)


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_manual_location_is_baked_and_changes_the_digest(dumps):
    """The UK drive case: no SEI, no event.json, nothing nearby → placed by
    hand through `locate`; the render bakes it and the digest changes."""
    from typer.testing import CliRunner
    from teslacam.cli import app
    _scan(dumps, exclude=("Dump3",))
    rd = dumps / "_review"
    eid = "Dump1|RecentClips|2025-08-01_15-00-16"
    dec.update(rd, eid, {"keep": "keep"})
    root, events = scan_mod.load_events(rd)
    before = render_mod.plan(events, dec.load_all(rd), dumps / "render")
    job0 = next(j for j in before if j.ev.id == eid)
    assert job0.ev.lat is None
    r = CliRunner().invoke(app, ["locate", "--root", str(dumps), eid, "55.65, -3.19", "--city", "Peebles"])
    assert r.exit_code == 0, r.output
    after = render_mod.plan(events, dec.load_all(rd), dumps / "render")
    job = next(j for j in after if j.ev.id == eid)
    assert job.ev.gps_source == "manual" and job.digest != job0.digest
    render_mod.render_job(root, job, model="", log=lambda s: None)
    t = json.loads(subprocess.run(["exiftool", "-j", "-n", "-Keys:GPSCoordinates", "-Keys:Description",
                                   str(job.out_dir / "front.mp4")], capture_output=True, text=True).stdout)[0]
    assert t["GPSCoordinates"].startswith("55.65 -3.19") and "Peebles" in t["Description"]
    r = CliRunner().invoke(app, ["locate", "--root", str(dumps), eid, "clear"])
    assert r.exit_code == 0 and "cleared" in r.output
    assert next(j for j in render_mod.plan(events, dec.load_all(rd), dumps / "render") if j.ev.id == eid).ev.lat is None
    r = CliRunner().invoke(app, ["locate", "--root", str(dumps), "Dump1|nope", "1, 2"])
    assert r.exit_code == 2


def test_parse_models():
    from teslacam.cli import _parse_models
    assert _parse_models([]) == ("", {})
    assert _parse_models(["Model X", "TeslaDump2023=Model Y"]) == ("Model X", {"TeslaDump2023": "Model Y"})


def test_precise_child_reencodes(dumps):
    _scan(dumps, exclude=("Dump3",))
    rd = dumps / "_review"
    eid = "Dump1|SavedClips|2026-09-09_19-08-30"
    dec.update(rd, eid, {"keep": "keep", "cameras": ["front"], "in_s": 1.0, "out_s": 3.0, "precise": True})
    root, events = scan_mod.load_events(rd)
    job = render_mod.plan(events, dec.load_all(rd), dumps / "render")[0]
    render_mod.render_job(root, job, model="", log=lambda s: None)
    f = _probe(job.out_dir / "front.mp4")
    assert float(f["format"]["duration"]) == pytest.approx(2.0, abs=0.6)


def test_cli_scan_and_dry_run(dumps):
    from typer.testing import CliRunner
    from teslacam.cli import app
    r = CliRunner().invoke(app, ["scan", "--root", str(dumps), "--no-thumbs", "--no-geocode", "--exclude", "Dump3"])
    assert r.exit_code == 0, r.output
    assert "Dump1" in r.output
    rd = dumps / "_review"
    dec.update(rd, "Dump1|SavedClips|2026-09-09_19-08-30", {"keep": "keep"})
    r = CliRunner().invoke(app, ["render", "--root", str(dumps), "--out", str(dumps / "render"), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "1 kept" in r.output and "2026-tesla-dashcam" in r.output
    assert not (dumps / "render").exists()


def test_drive_gps_falls_back_to_the_nearest_segment_with_a_fix(dumps, monkeypatch):
    """The Marrakesh drive case: the car had a fix only in its first minutes
    and writes no SEI at all once it has none, so the last segment — the one
    the scanner reads for drives — is empty → the nearest segment with a
    valid fix places the event instead of leaving it blank."""
    reads: list[str] = []

    def fake_sei(path, max_samples=100_000):
        reads.append(path.name)
        if path.name == "2025-08-01_15-00-16-front.mp4":
            return [{"lat": 31.622086, "lon": -8.001953, "speed_mps": 0.0}] * 3
        return []
    monkeypatch.setattr(scan_mod.sei_mod, "sei_track", fake_sei)
    doc = _scan(dumps, exclude=("Dump3",))
    by_id = {e["id"]: e for e in doc["events"]}
    d = by_id["Dump1|RecentClips|2025-08-01_15-00-16"]
    assert d["gps_source"] == "sei" and d["lat"] == pytest.approx(31.622086) and d["lon"] == pytest.approx(-8.001953)
    assert d["offset_source"] == "gps" and d["sei"] is True
    assert reads.index("2025-08-01_15-00-19-front.mp4") < reads.index("2025-08-01_15-00-16-front.mp4")
    # the single-segment drive an hour later has nothing of its own and
    # borrows the fix of this one (prev-drive)
    assert by_id["Dump1|RecentClips|2025-08-01_16-00-00"]["gps_source"] == "prev-drive"
