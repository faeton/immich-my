"""A camera that stops before its segment ends (2023 HW4 front: ~38 s of each
minute, real time, then nothing). The timeline follows the longest camera, the
hole stays a hole in every output, and nothing half-made is published."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from teslacam import decisions as dec
from teslacam import render as render_mod
from teslacam import scan as scan_mod

EID = "Car|SavedClips|2023-08-18_12-03-00"
CAMS4 = ("front", "back", "left_repeater", "right_repeater")


@pytest.fixture
def short_front(tmp_path: Path, ffmpeg) -> Path:
    """3 segments of 3 s; the front file of each covers only the first 1.5 s."""
    root = tmp_path / "dumps"
    ev = root / "Car" / "SavedClips" / "2023-08-18_12-03-00"
    for i, ts in enumerate(("2023-08-18_12-00-00", "2023-08-18_12-01-00", "2023-08-18_12-02-00")):
        for cam in CAMS4:
            ffmpeg(ev / f"{ts}-{cam}.mp4", seconds=1.5 if cam == "front" else 3.0,
                   utc=f"2023-08-18T10:0{i}:00")
    (ev / "event.json").write_text('{"timestamp":"2023-08-18T12:00:04","city":"Pristina","est_lat":"42.66",'
                                   '"est_lon":"21.16","reason":"user_interaction_dashcam_icon_tapped","camera":"0"}')
    return root


def _probe(p: Path) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json",
                                      str(p)], capture_output=True, text=True).stdout)


def _video_pts(p: Path) -> list[float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout
    return sorted(float(x) for x in out.split() if x.strip())


def _job(root: Path, decision: dict):
    scan_mod.scan(root, root / "_review", thumbs=False, geocode=False)
    rd = root / "_review"
    dec.update(rd, EID, {"keep": "keep", **decision})
    r, events = scan_mod.load_events(rd)
    return r, render_mod.plan(events, dec.load_all(rd), root / "render")[0]


def test_scan_timeline_follows_the_longest_camera(short_front):
    doc = scan_mod.scan(short_front, short_front / "_review", thumbs=False, geocode=False)
    ev = doc["events"][0]
    assert [s["d"] for s in ev["segs"]] == [pytest.approx(3.0, abs=0.05)] * 3
    assert [s["start"] for s in ev["segs"]] == [0.0, pytest.approx(3.0, abs=0.05), pytest.approx(6.0, abs=0.1)]
    assert all(s["cd"] == {"front": pytest.approx(1.5, abs=0.05)} for s in ev["segs"])
    assert ev["total"] == pytest.approx(9.0, abs=0.1) and "short-front" in ev["flags"]


def test_unaffected_events_keep_their_durations_and_json(dumps):
    """Cameras within a second of each other: the front length stays the segment
    length (old digests stay valid) and no `cd` key is written."""
    doc = scan_mod.scan(dumps, dumps / "_review", thumbs=False, geocode=False, exclude=("Dump3",))
    for ev in doc["events"]:
        assert "short-front" not in ev["flags"]
        assert all("cd" not in s for s in ev["segs"])


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_render_keeps_the_holes(short_front):
    root, job = _job(short_front, {"cameras": ["front", "back"], "in_s": 0.0, "out_s": 9.0})
    rec = render_mod.render_job(root, job, model="Model X", log=lambda s: None)
    out = job.out_dir
    # back: full window
    back = _probe(out / "back.mp4")
    assert float(back["format"]["duration"]) == pytest.approx(9.0, abs=0.3)
    # front: real time with gaps, ends where its footage ends (6.0 + 1.5)
    pts = _video_pts(out / "front.mp4")
    assert pts[-1] == pytest.approx(7.4, abs=0.2)
    gaps = [(a, b) for a, b in zip(pts, pts[1:]) if b - a > 0.5]
    assert [round(b, 1) for _, b in gaps] == [3.0, 6.0]
    assert rec["covered"]["front"] == [pytest.approx(0.0, abs=0.01), pytest.approx(7.5, abs=0.1)]
    # mosaic: full window, the front tile is black in a hole and live outside it
    m = _probe(out / "mosaic.mp4")
    assert float(m["format"]["duration"]) == pytest.approx(9.0, abs=0.3)
    assert _front_tile_luma(out / "mosaic.mp4", 2.2) < 25          # hole
    assert _front_tile_luma(out / "mosaic.mp4", 3.6) > 60          # footage (testsrc)
    assert _front_tile_luma(out / "mosaic.mp4", 8.6) < 25          # trailing hole: no frozen frame
    assert not list(out.glob("._*")) and not list(out.glob("*.part.mp4"))


def _front_tile_luma(p: Path, t: float) -> float:
    """Mean luma of the top-left (front) tile at `t`, ignoring the label corner."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(p), "-frames:v", "1",
                          "-vf", "crop=iw/2:ih/2:0:0,crop=iw*0.6:ih*0.4:iw*0.2:ih*0.55,signalstats,"
                                 "metadata=print:key=lavfi.signalstats.YAVG:file=-",
                          "-f", "null", "-"], capture_output=True, text=True).stdout
    return float(out.split("YAVG=")[1].split()[0])


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_window_starting_in_a_hole(short_front):
    """In point inside the first segment's front hole: the front child starts at
    the next segment, the mosaic still covers the whole window."""
    root, job = _job(short_front, {"cameras": ["front", "back"], "in_s": 2.0, "out_s": 4.5})
    rec = render_mod.render_job(root, job, model="", log=lambda s: None)
    assert rec["covered"]["front"][0] == pytest.approx(3.0, abs=0.05)
    assert rec["covered"]["front"][1] == pytest.approx(4.5, abs=0.3)
    m = _probe(job.out_dir / "mosaic.mp4")
    assert float(m["format"]["duration"]) == pytest.approx(2.5, abs=0.3)
    assert _front_tile_luma(job.out_dir / "mosaic.mp4", 0.4) < 25     # leading hole (2.0–3.0)
    assert _front_tile_luma(job.out_dir / "mosaic.mp4", 1.4) > 60     # footage from 3.0


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_window_entirely_in_a_hole_skips_the_camera(short_front):
    root, job = _job(short_front, {"cameras": ["front", "back"], "in_s": 1.7, "out_s": 2.9})
    logs: list[str] = []
    rec = render_mod.render_job(root, job, model="", log=logs.append)
    assert rec["children"] == [f"{job.slug}/back.mp4"]
    assert any("skipped: front" in s for s in logs)


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_missing_middle_file_is_a_gap_not_a_splice(short_front):
    ev = short_front / "Car" / "SavedClips" / "2023-08-18_12-03-00"
    (ev / "2023-08-18_12-01-00-back.mp4").unlink()
    root, job = _job(short_front, {"cameras": ["back"], "in_s": 0.0, "out_s": 9.0})
    render_mod.render_job(root, job, model="", log=lambda s: None)
    pts = _video_pts(job.out_dir / "back.mp4")
    assert pts[-1] == pytest.approx(8.9, abs=0.2)          # not 5.9: the third file stays at 6.0
    assert any(b - a > 2.5 for a, b in zip(pts, pts[1:]))


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool missing")
def test_a_failed_event_publishes_nothing(short_front, monkeypatch):
    root, job = _job(short_front, {"cameras": ["front", "back"], "in_s": 0.0, "out_s": 9.0})
    real = render_mod._verify

    def bad(path, expected_s, *, tolerance):
        if "back" in path.name:
            raise RuntimeError("back: duration 14.4s, expected 9.0s")
        return real(path, expected_s, tolerance=tolerance)

    monkeypatch.setattr(render_mod, "_verify", bad)
    with pytest.raises(RuntimeError, match="back"):
        render_mod.render_job(root, job, model="", log=lambda s: None)
    assert not list(job.out_dir.glob("*.mp4")) and not (job.out_dir / "render.json").exists()
