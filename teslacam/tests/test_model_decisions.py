import json

import pytest

from teslacam import decisions as dec
from teslacam.model import Event, Segment, clamp_window, default_window, reason_slug


def _ev(kind="saved", trigger=170.0, n=3, d=60.0):
    segs = [Segment(t=f"2026-09-09_19-0{i}-00", d=d, start=i * d, f={"front": f"{i}-front.mp4", "back": f"{i}-back.mp4"},
                    utc=f"2026-09-09T17:0{i}:00") for i in range(n)]
    return Event(id="D|SavedClips|x", dump="D", kind=kind, dir="D/SavedClips/x", cams=["front", "back"], segs=segs,
                 total=n * d, trigger_s=None if kind == "recent" else trigger, ts="2026-09-09T19:02:50", offset="+02:00",
                 offset_source="gps", lat=1.0, lon=2.0, gps_source="json", city="X", reason="user_interaction_honk",
                 reason_slug="honk", sei=False, bad=0, thumb="t.jpg")


def test_default_windows():
    assert default_window(_ev()) == (0.0, 180.0)            # 170-180 clipped, 170+30 clipped to total
    assert default_window(_ev(kind="sentry", trigger=100)) == (40.0, 160.0)
    assert default_window(_ev(kind="recent")) == (0.0, 180.0)


def test_clamp_and_snap():
    ev = _ev()
    assert clamp_window(ev, 70, 130) == (70.0, 130.0)
    assert clamp_window(ev, 130, 70) == default_window(ev)      # inverted → default
    assert clamp_window(ev, -5, 999) == (0.0, 180.0)
    assert ev.snapped(70, 130) == (60.0, 180.0)
    assert [s.t for s in ev.overlapping(70, 130)] == ["2026-09-09_19-01-00", "2026-09-09_19-02-00"]


def test_utc_at_uses_segment_clock():
    ev = _ev()
    assert ev.utc_at(75.0).isoformat() == "2026-09-09T17:01:15"


def test_reason_slugs():
    assert reason_slug("user_interaction_honk", "saved") == "honk"
    assert reason_slug("sentry_aware_object_detection", "sentry") == "sentry"
    assert reason_slug(None, "sentry") == "sentry"
    assert reason_slug(None, "recent") == "drive"
    assert reason_slug("something_new", "saved") == "unknown"


def test_decision_roundtrip_and_validate(tmp_path):
    d = dec.update(tmp_path, "D|SavedClips|x", {"keep": "keep", "in_s": 10, "tags": [" crash ", ""]})
    assert d.keep == "keep" and d.in_s == 10.0 and d.tags == ["crash"] and d.updated
    again = dec.load_all(tmp_path)["D|SavedClips|x"]
    assert again.keep == "keep"
    assert json.loads((tmp_path / "decisions-D.json").read_text())["dump"] == "D"
    with pytest.raises(ValueError):
        dec.validate({"keep": "maybe"})
    with pytest.raises(ValueError):
        dec.validate({"bogus": 1})
    with pytest.raises(ValueError):
        dec.validate({"updated": "x"})


def test_effective_cameras():
    ev = _ev()
    a, b, cams = dec.effective(ev, dec.Decision(cameras=["back", "nope"]))
    assert cams == ["back"] and (a, b) == default_window(ev)


def test_seed_from_tags_json(tmp_path):
    old = tmp_path / "tags.json"
    old.write_text(json.dumps({"D|SavedClips|x": {"tags": ["crash"], "note": "hit", "in": 5, "out": 50},
                               "D|SavedClips|y": {}}))
    assert dec.seed_from_tags_json(tmp_path, old) == 2
    got = dec.load_all(tmp_path)
    assert got["D|SavedClips|x"].keep == "keep" and got["D|SavedClips|x"].out_s == 50
    assert got["D|SavedClips|y"].keep == "unreviewed"
