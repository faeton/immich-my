import json
import threading

import pytest

from cull import decisions as dec

ITEMS = {"S/a.MP4": {"id": "S/a.MP4", "fp": "f1"}, "S/b.MP4": {"id": "S/b.MP4", "fp": "f2"}}


def test_defaults_and_keep_records_fingerprint(tmp_path):
    assert dec.effective(ITEMS["S/a.MP4"], None) == {**dec.DEFAULT, "changed": False}
    out = dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"keep": "keep"}, set())
    assert out["S/a.MP4"]["keep"] == "keep" and out["S/a.MP4"]["fp"] == "f1" and not out["S/a.MP4"]["changed"]
    on_disk = json.loads((tmp_path / "_cull" / "decisions.json").read_text())
    assert on_disk["S/a.MP4"]["keep"] == "keep"


def test_changed_when_file_changes_after_review(tmp_path):
    dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"keep": "drop"}, set())
    d = dec.load(tmp_path)["S/a.MP4"]
    assert dec.effective({"id": "S/a.MP4", "fp": "OTHER"}, d)["changed"]
    back = dec.apply(tmp_path, {"S/a.MP4": {"id": "S/a.MP4", "fp": "OTHER"}}, ["S/a.MP4"], {"keep": "keep"}, set())
    assert not back["S/a.MP4"]["changed"]


def test_trip_tags_note_on_many(tmp_path):
    ids = list(ITEMS)
    dec.apply(tmp_path, ITEMS, ids, {"trip": "2022-01-kyiv", "add_tags": ["Drone", " drone ", ""]}, set())
    out = dec.apply(tmp_path, ITEMS, ids, {"add_tags": ["sunset"], "note": " nice "}, set())
    assert all(o["trip"] == "2022-01-kyiv" and o["tags"] == ["Drone", "sunset"] and o["note"] == "nice"
               for o in out.values())
    out = dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"trip": None, "tags": []}, set())
    assert out["S/a.MP4"]["trip"] is None and out["S/a.MP4"]["tags"] == []


@pytest.mark.parametrize("patch,msg", [
    ({"keep": "maybe"}, "keep"),
    ({"trip": "Kyiv trip"}, "trip"),
    ({"trip": "2022-1-kyiv"}, "trip"),
    ({"note": 5}, "note"),
    ({"tags": "x"}, "tags"),
    ({"bogus": 1}, "unknown"),
])
def test_bad_patches(tmp_path, patch, msg):
    with pytest.raises(ValueError, match=msg):
        dec.apply(tmp_path, ITEMS, ["S/a.MP4"], patch, set())


def test_existing_trip_names_are_accepted(tmp_path):
    out = dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"trip": "2023-08-Croatia_old"}, {"2023-08-Croatia_old"})
    assert out["S/a.MP4"]["trip"] == "2023-08-Croatia_old"
    assert dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"trip": "2023-08-14-split"}, set())["S/a.MP4"]["trip"]


def test_unknown_ids_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown item"):
        dec.apply(tmp_path, ITEMS, ["S/zzz.MP4"], {"keep": "keep"}, set())


def test_concurrent_posts_lose_nothing(tmp_path):
    items = {f"S/{i}.MP4": {"id": f"S/{i}.MP4", "fp": "f"} for i in range(40)}
    threads = [threading.Thread(target=dec.apply, args=(tmp_path, items, [k], {"keep": "keep"}, set()))
               for k in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(d["keep"] == "keep" for d in dec.load(tmp_path).values()) == 40


def test_list_trips(tmp_path):
    (tmp_path / "2023-08-croatia").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    assert dec.list_trips(tmp_path) == ["2023-08-croatia"]
    assert dec.list_trips(None) == [] and dec.list_trips(tmp_path / "nope") == []


def test_trip_name_must_fullmatch(tmp_path):
    with pytest.raises(ValueError):
        dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"trip": "2024-12-prague\n"}, set())
    dec.apply(tmp_path, ITEMS, ["S/a.MP4"], {"trip": "2024-12-prague"}, set())
