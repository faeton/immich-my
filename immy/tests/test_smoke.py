from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immy.cli import _fmt_date, _fmt_gps, _fmt_make_model, app
from immy.exif import ExifRow, iter_media

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _xmp_tags(xmp: Path) -> dict[str, str]:
    out = subprocess.run(
        ["exiftool", "-j", "-n", "-G0", str(xmp)],
        capture_output=True, text=True, check=True,
    )
    import json
    blobs = json.loads(out.stdout)
    return blobs[0] if blobs else {}


@pytest.fixture
def dji_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    return target


@pytest.fixture
def insta360_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "insta360-pair"
    shutil.copytree(FIXTURES / "insta360-pair", target)
    return target


@pytest.fixture
def trip_anchor_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "trip-anchor-simple"
    shutil.copytree(FIXTURES / "trip-anchor-simple", target)
    return target


@pytest.fixture
def clock_drift_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "clock-drift-simple"
    shutil.copytree(FIXTURES / "clock-drift-simple", target)
    return target


@pytest.fixture
def tag_suggest_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "tag-suggest-missing"
    shutil.copytree(FIXTURES / "tag-suggest-missing", target)
    return target


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "audit" in result.stdout
    assert "promote" in result.stdout


def test_fmt_date_marks_xmp_source():
    row = ExifRow(
        path=Path("IMG_0001.JPG"),
        raw={
            "EXIF:DateTimeOriginal": "2024:02:19 10:21:04",
            "XMP:DateTimeOriginal": "2024:02:19 10:21:04-04:00",
        },
    )
    assert _fmt_date(row) == "2024:02:19 10:21:04-04:00 (xmp)"


def test_fmt_gps_shows_xmp_source():
    row = ExifRow(
        path=Path("IMG_0001.JPG"),
        raw={
            "XMP:GPSLatitude": -16.28930949421266,
            "XMP:GPSLongitude": -67.82720498551421,
        },
    )
    assert _fmt_gps(row) == "-16.2893,-67.8272 (xmp)"


def test_fmt_make_model_uses_quicktime_android_fields():
    row = ExifRow(
        path=Path("VID_0001.mp4"),
        raw={
            "QuickTime:AndroidMake": "Xiaomi",
            "QuickTime:AndroidModel": "2303ERA42L",
        },
    )
    assert _fmt_make_model(row) == "Xiaomi 2303ERA42L"


def test_fmt_make_model_falls_back_to_xmp_camera_tag():
    row = ExifRow(
        path=Path("VID_0001.mp4"),
        raw={
            "XMP:HierarchicalSubject": [
                "Events/Trip",
                "Gear/Camera/Xiaomi 2303ERA42L",
                "Source/VID",
            ],
        },
    )
    assert _fmt_make_model(row) == "Xiaomi 2303ERA42L (xmp)"


def test_audit_empty_folder_exits_zero(tmp_path):
    result = runner.invoke(app, ["audit", str(tmp_path)])
    assert result.exit_code == 0
    assert "0 media file" in result.stdout


def test_iter_media_skips_audit_derivatives(tmp_path: Path):
    trip = tmp_path / "trip"
    trip.mkdir()
    source = trip / "IMG_0001.JPG"
    source.write_bytes(b"x")
    derived = trip / ".audit" / "derivatives" / "thumbs" / "u" / "ab" / "cd" / "generated_preview.jpeg"
    derived.parent.mkdir(parents=True)
    derived.write_bytes(b"x")

    found = [p.relative_to(trip).as_posix() for p in iter_media(trip)]
    assert found == ["IMG_0001.JPG"]


def test_dji_fixture_read_only_no_writes(dji_fixture: Path):
    result = runner.invoke(app, ["audit", str(dji_fixture), "--auto"])
    assert result.exit_code == 0, result.stdout
    # No XMP sidecar without --write.
    assert not (dji_fixture / "DJI_0001.xmp").exists()
    # state.yml not created on a pure read.
    assert not (dji_fixture / ".audit" / "state.yml").exists()
    # notes file is created even on read (it's independent of --write).
    assert (dji_fixture / "README.md").is_file()


def test_dji_fixture_write_gps_and_date_from_srt(dji_fixture: Path):
    result = runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout

    xmp = dji_fixture / "DJI_0001.xmp"
    assert xmp.is_file()

    tags = _xmp_tags(xmp)
    assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.29627, abs=1e-5)
    assert float(tags["XMP:GPSLongitude"]) == pytest.approx(57.40794, abs=1e-5)
    # trip-timezone-guess-gps spots the Casela SRT coords → Indian/Mauritius
    # → trip-timezone cascades +04:00 into the capture datetime.
    assert tags["XMP:DateTimeOriginal"] == "2026:03:05 09:49:01+04:00"

    state = dji_fixture / ".audit" / "state.yml"
    assert state.is_file()
    log = dji_fixture / ".audit" / "audit.jsonl"
    assert log.is_file()
    # Five rules fire: dji-gps-from-srt, dji-date-from-srt,
    # trip-tags-from-notes, trip-timezone-guess-gps, trip-timezone.
    assert len(log.read_text().splitlines()) == 5


def test_dji_fixture_idempotent(dji_fixture: Path):
    runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    xmp_mtime = (dji_fixture / "DJI_0001.xmp").stat().st_mtime
    log_lines_before = len((dji_fixture / ".audit" / "audit.jsonl").read_text().splitlines())

    result = runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    assert result.exit_code == 0
    assert "0 pending" in result.stdout
    assert (dji_fixture / "DJI_0001.xmp").stat().st_mtime == xmp_mtime
    log_lines_after = len((dji_fixture / ".audit" / "audit.jsonl").read_text().splitlines())
    assert log_lines_after == log_lines_before


def test_insta360_pair_recorded(insta360_fixture: Path):
    result = runner.invoke(app, ["audit", str(insta360_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout

    import yaml
    state = yaml.safe_load((insta360_fixture / ".audit" / "state.yml").read_text())
    applied = state["applied"]

    vid = "VID_20240101_120000_00_001.insv"
    lrv = "LRV_20240101_120000_01_001.lrv"
    assert "insta360-pair-by-ts-serial" in applied[vid]
    assert "insta360-pair-by-ts-serial" in applied[lrv]
    # VID also gets date-from-filename.
    assert "date-from-filename-vid-img" in applied[vid]
    assert (insta360_fixture / f"{Path(vid).stem}.xmp").is_file()


def test_trip_gps_anchor_applies_to_all_gps_less_files(trip_anchor_fixture: Path):
    result = runner.invoke(app, ["audit", str(trip_anchor_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    for name in ("IMG_A", "IMG_B"):
        xmp = trip_anchor_fixture / f"{name}.xmp"
        assert xmp.is_file()
        tags = _xmp_tags(xmp)
        assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.29627, abs=1e-5)
        assert float(tags["XMP:GPSLongitude"]) == pytest.approx(57.40794, abs=1e-5)


def test_interactive_prompt_writes_coords_to_notes(trip_anchor_fixture: Path):
    # Strip coords from the fixture's TRIP.md; simulate the "no anchor yet" case.
    from immy.notes import parse_frontmatter, update_frontmatter
    notes = trip_anchor_fixture / "TRIP.md"
    import yaml
    text = notes.read_text()
    # Remove the coords line by parsing+rewriting explicitly.
    fm = parse_frontmatter(notes)
    fm["location"].pop("coords", None)
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else ""
    notes.write_text("---\n" + yaml.safe_dump(fm) + "---\n" + body)

    result = runner.invoke(
        app,
        ["audit", str(trip_anchor_fixture), "--write"],
        input="-20.29627, 57.40794\n",
    )
    assert result.exit_code == 0, result.stdout
    fm_after = parse_frontmatter(notes)
    assert fm_after["location"]["coords"] == [-20.29627, 57.40794]
    # And the coords got applied to the XMPs.
    tags = _xmp_tags(trip_anchor_fixture / "IMG_A.xmp")
    assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.29627, abs=1e-5)


def test_scaffolded_notes_contain_suggested_tags_and_location_stub(dji_fixture: Path):
    runner.invoke(app, ["audit", str(dji_fixture), "--auto"])
    import yaml
    notes_path = dji_fixture / "README.md"
    assert notes_path.is_file()
    text = notes_path.read_text()
    assert text.startswith("---")
    yaml_block = text.split("\n---\n", 2)[0][3:].lstrip()
    fm = yaml.safe_load(yaml_block)
    assert "location" in fm
    assert fm["location"]["coords"] is None
    assert any(t.startswith("Events/") for t in fm["tags"])
    assert any(t.startswith("Source/") for t in fm["tags"])


def test_tags_from_notes_written_to_xmp(dji_fixture: Path):
    import yaml
    # Pre-write a notes file with explicit tags so trip-tags-from-notes fires.
    notes = dji_fixture / "TRIP.md"
    notes.write_text(
        "---\n"
        + yaml.safe_dump({
            "trip": "dji-srt-pair",
            "tags": ["Events/MyTrip", "Gear/Camera/DJI Mini", "Source/DJI"],
        })
        + "---\n# x\n"
    )
    result = runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(dji_fixture / "DJI_0001.xmp")
    h = tags.get("XMP:HierarchicalSubject")
    s = tags.get("XMP:Subject")
    assert isinstance(h, list)
    assert set(h) == {"Events/MyTrip", "Gear/Camera/DJI Mini", "Source/DJI"}
    assert isinstance(s, list)
    assert "MyTrip" in s and "DJI" in s


def test_sibling_srt_beats_trip_anchor_for_gps(dji_fixture: Path):
    # Give the DJI fixture a TRIP.md with a *different* anchor. The SRT's GPS
    # must win on DJI_0001.JPG because dji-gps-from-srt is registered first.
    (dji_fixture / "TRIP.md").write_text(
        "---\nlocation:\n  coords: [10.0, 20.0]\n---\n# x\n"
    )
    result = runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(dji_fixture / "DJI_0001.xmp")
    assert float(tags["XMP:GPSLatitude"]) == pytest.approx(-20.29627, abs=1e-5)
    assert float(tags["XMP:GPSLongitude"]) == pytest.approx(57.40794, abs=1e-5)


def test_trip_timezone_suffixes_datetime_original(dji_fixture: Path):
    # TRIP.md specifies Indian/Mauritius (UTC+04:00, no DST).
    # Pass 1: dji-date-from-srt writes naive DateTimeOriginal.
    # Pass 2 (after re-read): trip-timezone rewrites it with +04:00 suffix.
    (dji_fixture / "TRIP.md").write_text(
        "---\ntimezone: Indian/Mauritius\nlocation:\n  coords: [-20.3, 57.4]\n---\n# x\n"
    )
    result = runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(dji_fixture / "DJI_0001.xmp")
    dt = tags.get("XMP:DateTimeOriginal", "")
    assert dt.endswith("+04:00"), dt


def test_trip_timezone_noop_when_no_date(tmp_path: Path):
    # A folder whose files have no date → trip-timezone writes nothing.
    import yaml as _yaml
    target = tmp_path / "tz-nodate"
    shutil.copytree(FIXTURES / "trip-anchor-simple", target)
    notes = target / "TRIP.md"
    fm = {"timezone": "UTC", "location": {"coords": [0.0, 0.0]}}
    notes.write_text("---\n" + _yaml.safe_dump(fm) + "---\n# x\n")
    result = runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    tags = _xmp_tags(target / "IMG_A.xmp")
    assert "XMP:GPSLatitude" in tags
    assert "XMP:DateTimeOriginal" not in tags


def test_clock_drift_flags_outlier_but_read_only_does_not_write(clock_drift_fixture: Path):
    result = runner.invoke(app, ["audit", str(clock_drift_fixture), "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "MEDIUM findings: 1 pending review" in result.stdout
    assert "clock-drift" in result.stdout
    assert not (clock_drift_fixture / "DSC_0004.xmp").exists()


def test_clock_drift_yes_medium_writes_median_and_reaudit_clean(clock_drift_fixture: Path):
    result = runner.invoke(
        app, ["audit", str(clock_drift_fixture), "--write", "--auto", "--yes-medium"],
    )
    assert result.exit_code == 0, result.stdout
    xmp = clock_drift_fixture / "DSC_0004.xmp"
    assert xmp.is_file()
    tags = _xmp_tags(xmp)
    # Median of 10:00, 10:05, 10:10, 12:00(+4d) on 4 samples is the avg of
    # the middle two sorted timestamps: (10:05 + 10:10)/2 = 10:07:30 on apr 1.
    assert tags["XMP:DateTimeOriginal"] == "2026:04:01 10:07:30"

    # Re-audit is a no-op: MEDIUM finding disappears (XMP override now puts
    # DSC_0004 within 24h of the new median).
    result2 = runner.invoke(
        app, ["audit", str(clock_drift_fixture), "--write", "--auto", "--yes-medium"],
    )
    assert result2.exit_code == 0
    assert "MEDIUM findings" not in result2.stdout
    assert "review clock-drift" not in result2.stdout


def test_clock_drift_interactive_y_applies_n_skips(clock_drift_fixture: Path):
    # LOW coords + tz prompts fire first; send empty to skip both, then "y"
    # to accept the single MEDIUM clock-drift finding.
    result = runner.invoke(
        app, ["audit", str(clock_drift_fixture), "--write"],
        input="\n\ny\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "apply? [y/N]" in result.stdout
    assert (clock_drift_fixture / "DSC_0004.xmp").is_file()


def test_clock_drift_interactive_n_leaves_pending(tmp_path: Path):
    target = tmp_path / "clock-drift-simple"
    shutil.copytree(FIXTURES / "clock-drift-simple", target)
    result = runner.invoke(
        app, ["audit", str(target), "--write"],
        input="\n\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert not (target / "DSC_0004.xmp").exists()


def test_clock_drift_skipped_without_yes_medium_in_auto(clock_drift_fixture: Path):
    # --auto without --yes-medium: report but do not apply.
    result = runner.invoke(app, ["audit", str(clock_drift_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "MEDIUM findings: 1 pending review" in result.stdout
    assert not (clock_drift_fixture / "DSC_0004.xmp").exists()


def test_clock_drift_skipped_below_min_samples(trip_anchor_fixture: Path):
    # trip-anchor-simple has 2 media files — under MIN_SAMPLES=3, clock-drift
    # must not propose anything even if the files have no EXIF dates.
    result = runner.invoke(app, ["audit", str(trip_anchor_fixture), "--auto"])
    assert result.exit_code == 0
    assert "review clock-drift" not in result.stdout
    assert "MEDIUM findings" not in result.stdout


def test_tag_suggest_flags_missing_categories(tag_suggest_fixture: Path):
    result = runner.invoke(app, ["audit", str(tag_suggest_fixture), "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "review tag-suggest-missing: 1 file(s)" in result.stdout
    # No write without --write: notes unchanged.
    import yaml
    fm = yaml.safe_load(
        (tag_suggest_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert fm["tags"] == ["Events/CustomEventLabel"]


def test_tag_suggest_yes_medium_merges_into_notes_and_cascades_to_xmp(tag_suggest_fixture: Path):
    result = runner.invoke(
        app, ["audit", str(tag_suggest_fixture), "--write", "--auto", "--yes-medium"],
    )
    assert result.exit_code == 0, result.stdout

    import yaml
    fm = yaml.safe_load(
        (tag_suggest_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert "Events/CustomEventLabel" in fm["tags"]  # user's existing tag preserved
    assert "Gear/Camera/NIKON CORPORATION NIKON Z50_2" in fm["tags"]
    assert "Source/DSC" in fm["tags"]

    # Cascade: trip-tags-from-notes re-fires after the notes write and
    # lands the full set in the XMP sidecar.
    tags = _xmp_tags(tag_suggest_fixture / "DSC_0001.xmp")
    h = tags.get("XMP:HierarchicalSubject")
    assert isinstance(h, list)
    assert "Gear/Camera/NIKON CORPORATION NIKON Z50_2" in h
    assert "Source/DSC" in h


def test_tag_suggest_respects_opt_out(tag_suggest_fixture: Path):
    notes = tag_suggest_fixture / "TRIP.md"
    notes.write_text(
        "---\ntrip: x\ntag_suggestions: off\ntags:\n  - Events/Keep\n---\n# x\n"
    )
    result = runner.invoke(app, ["audit", str(tag_suggest_fixture), "--auto"])
    assert result.exit_code == 0
    assert "review tag-suggest-missing" not in result.stdout
    assert "MEDIUM findings" not in result.stdout


def test_tag_suggest_idempotent_after_accept(tag_suggest_fixture: Path):
    runner.invoke(
        app, ["audit", str(tag_suggest_fixture), "--write", "--auto", "--yes-medium"],
    )
    # Second run: no MEDIUM pending, re-audit clean.
    result = runner.invoke(
        app, ["audit", str(tag_suggest_fixture), "--write", "--auto", "--yes-medium"],
    )
    assert result.exit_code == 0
    assert "review tag-suggest-missing" not in result.stdout
    assert "0 pending" in result.stdout


def _seed_clock_drift_with_coords(folder: Path) -> None:
    """clock-drift-simple has EXIF dates but a minimal TRIP.md. Prefill
    location.coords so the tz prompt is the only one that fires."""
    import yaml
    notes = folder / "TRIP.md"
    fm = yaml.safe_load(notes.read_text().split("---", 2)[1]) or {}
    fm["location"] = {"coords": [-20.3, 57.4]}
    notes.write_text("---\n" + yaml.safe_dump(fm) + "---\n# x\n")


def test_interactive_tz_prompt_writes_zone_to_notes(clock_drift_fixture: Path):
    # No coords → tz-guess-gps stays silent; prompt is the only source.
    # Pipe: empty coords, zone, n for clock-drift MEDIUM.
    import yaml
    result = runner.invoke(
        app,
        ["audit", str(clock_drift_fixture), "--write"],
        input="\nIndian/Mauritius\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    fm = yaml.safe_load(
        (clock_drift_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert fm["timezone"] == "Indian/Mauritius"
    tags = _xmp_tags(clock_drift_fixture / "DSC_0001.xmp")
    assert tags["XMP:DateTimeOriginal"].endswith("+04:00")


def test_interactive_tz_prompt_rejects_unknown_zone(clock_drift_fixture: Path):
    # No coords → no guess-gps path; rejection must leave notes empty.
    import yaml
    result = runner.invoke(
        app,
        ["audit", str(clock_drift_fixture), "--write"],
        input="\nNot/A/Real/Zone\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "unknown zone" in result.stdout
    fm = yaml.safe_load(
        (clock_drift_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert fm.get("timezone") in (None, "")


def test_interactive_tz_prompt_skipped_when_coords_resolve_zone(clock_drift_fixture: Path):
    import yaml
    _seed_clock_drift_with_coords(clock_drift_fixture)
    result = runner.invoke(
        app,
        ["audit", str(clock_drift_fixture), "--write"],
        input="n\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "Enter IANA zone" not in result.stdout
    fm = yaml.safe_load(
        (clock_drift_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert fm["timezone"] == "Indian/Mauritius"


def test_tz_prompt_skipped_when_already_set(clock_drift_fixture: Path):
    import yaml
    notes = clock_drift_fixture / "TRIP.md"
    fm = yaml.safe_load(notes.read_text().split("---", 2)[1]) or {}
    fm["timezone"] = "UTC"
    fm["location"] = {"coords": [0.0, 0.0]}
    notes.write_text("---\n" + yaml.safe_dump(fm) + "---\n# x\n")
    result = runner.invoke(app, ["audit", str(clock_drift_fixture), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "has no timezone" not in result.stdout


def test_tz_guess_from_gps_writes_zone_to_notes(tmp_path: Path):
    # A fixture with EXIF GPS but no `timezone:` in notes. tz-guess-gps
    # should reverse-lookup the zone and write it to notes; trip-timezone
    # then cascades the offset into XMP.
    import yaml
    target = tmp_path / "tz-gps"
    target.mkdir()
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    dst = target / "IMG_0001.JPG"
    dst.write_bytes(src.read_bytes())
    # Mauritius: -20.3, 57.4
    subprocess.run([
        "exiftool", "-overwrite_original",
        "-EXIF:GPSLatitudeRef=S", "-EXIF:GPSLatitude=20.3",
        "-EXIF:GPSLongitudeRef=E", "-EXIF:GPSLongitude=57.4",
        "-EXIF:DateTimeOriginal=2026:04:01 10:00:00",
        str(dst),
    ], check=True, capture_output=True)
    (target / "TRIP.md").write_text("---\ntrip: tz-gps\n---\n")

    result = runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout

    fm = yaml.safe_load((target / "TRIP.md").read_text().split("---", 2)[1])
    assert fm["timezone"] == "Indian/Mauritius"
    tags = _xmp_tags(target / "IMG_0001.xmp")
    assert tags["XMP:DateTimeOriginal"].endswith("+04:00")


def test_tz_guess_from_notes_coords_writes_zone_to_notes(tmp_path: Path):
    import yaml
    target = tmp_path / "tz-notes-coords"
    target.mkdir()
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    dst = target / "IMG_0001.JPG"
    dst.write_bytes(src.read_bytes())
    subprocess.run([
        "exiftool", "-overwrite_original",
        "-EXIF:DateTimeOriginal=2026:04:01 10:00:00",
        str(dst),
    ], check=True, capture_output=True)
    (target / "TRIP.md").write_text(
        "---\n"
        "trip: tz-notes-coords\n"
        "location:\n"
        "  coords: [-16.28930949421266, -67.82720498551421]\n"
        "---\n"
    )

    result = runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout

    fm = yaml.safe_load((target / "TRIP.md").read_text().split("---", 2)[1])
    assert fm["timezone"] == "America/La_Paz"
    tags = _xmp_tags(target / "IMG_0001.xmp")
    assert tags["XMP:DateTimeOriginal"].endswith("-04:00")


def test_tz_guess_respects_existing_timezone(tmp_path: Path):
    # Pre-set timezone in notes → guess-gps must stand down even with GPS.
    import yaml
    target = tmp_path / "tz-keep"
    target.mkdir()
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    dst = target / "IMG_0001.JPG"
    dst.write_bytes(src.read_bytes())
    subprocess.run([
        "exiftool", "-overwrite_original",
        "-EXIF:GPSLatitudeRef=S", "-EXIF:GPSLatitude=20.3",
        "-EXIF:GPSLongitudeRef=E", "-EXIF:GPSLongitude=57.4",
        "-EXIF:DateTimeOriginal=2026:04:01 10:00:00",
        str(dst),
    ], check=True, capture_output=True)
    (target / "TRIP.md").write_text(
        "---\ntimezone: Europe/Madrid\n---\n"
    )
    result = runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    fm = yaml.safe_load((target / "TRIP.md").read_text().split("---", 2)[1])
    assert fm["timezone"] == "Europe/Madrid"


def test_trip_timezone_respects_per_file_offset(tmp_path: Path):
    # iPhone-style EXIF: OffsetTimeOriginal present. trip-timezone HIGH
    # must skip these files so the camera's offset wins over the trip zone.
    target = tmp_path / "per-file-offset"
    target.mkdir()
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    dst = target / "IMG_0001.JPG"
    dst.write_bytes(src.read_bytes())
    subprocess.run([
        "exiftool", "-overwrite_original",
        "-EXIF:DateTimeOriginal=2026:04:01 10:00:00",
        "-EXIF:OffsetTimeOriginal=-05:00",   # file claims New York offset
        str(dst),
    ], check=True, capture_output=True)
    (target / "TRIP.md").write_text(
        "---\ntimezone: Indian/Mauritius\n---\n"   # trip zone would be +04:00
    )
    result = runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    assert result.exit_code == 0, result.stdout
    # trip-timezone must have stood down — no XMP:DateTimeOriginal written.
    xmp = target / "IMG_0001.xmp"
    if xmp.exists():
        tags = _xmp_tags(xmp)
        assert "XMP:DateTimeOriginal" not in tags, tags


def test_export_date_trap_flags_file_missing_dto(tmp_path: Path):
    # Fresh folder: copy one IMG_A.JPG, strip DateTimeOriginal, set ModifyDate.
    target = tmp_path / "export-trap"
    target.mkdir()
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    dst = target / "edit_2025.jpg"
    dst.write_bytes(src.read_bytes())
    subprocess.run([
        "exiftool", "-overwrite_original",
        "-EXIF:DateTimeOriginal=",
        "-EXIF:CreateDate=",
        "-EXIF:ModifyDate=2026:04:10 15:00:00",
        str(dst),
    ], check=True, capture_output=True)

    result = runner.invoke(app, ["audit", str(target), "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "export-date-trap" in result.stdout


def test_export_date_trap_not_flagged_when_dto_present(dji_fixture: Path):
    # DJI JPG has EXIF:DateTimeOriginal + ModifyDate after audit writes SRT
    # date. Not a trap — don't flag it.
    runner.invoke(app, ["audit", str(dji_fixture), "--write", "--auto"])
    result = runner.invoke(app, ["audit", str(dji_fixture), "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "export-date-trap" not in result.stdout


def _build_two_camera_folder(
    root: Path, *,
    cam_a: tuple[str, str] = ("NIKON", "Z50_2"),
    cam_b: tuple[str, str] = ("SONY", "ILCE-7M4"),
    offset_seconds: int = 3 * 3600,
    count: int = 4,
) -> Path:
    """Stamp `count` JPGs per camera with matching EXIF. Camera B's dates
    are shifted by `offset_seconds` (camera B is "behind" when positive)."""
    target = root / "two-cam"
    target.mkdir()
    src = FIXTURES / "trip-anchor-simple" / "IMG_A.JPG"
    base_ts = "2026:04:01 10:00:00"
    from datetime import datetime, timedelta
    base_dt = datetime.strptime(base_ts, "%Y:%m:%d %H:%M:%S")

    def stamp(path: Path, make: str, model: str, dt: datetime) -> None:
        path.write_bytes(src.read_bytes())
        subprocess.run([
            "exiftool", "-overwrite_original",
            f"-EXIF:Make={make}",
            f"-EXIF:Model={model}",
            f"-EXIF:DateTimeOriginal={dt.strftime('%Y:%m:%d %H:%M:%S')}",
            str(path),
        ], check=True, capture_output=True)

    for i in range(count):
        ts = base_dt + timedelta(minutes=5 * i)
        stamp(target / f"A_{i:04d}.JPG", cam_a[0], cam_a[1], ts)
        ts_b = ts - timedelta(seconds=offset_seconds)
        stamp(target / f"B_{i:04d}.JPG", cam_b[0], cam_b[1], ts_b)

    (target / "TRIP.md").write_text("---\ntrip: two-cam\n---\n# x\n")
    return target


def test_clock_drift_by_camera_flags_offset_group(tmp_path: Path):
    folder = _build_two_camera_folder(tmp_path)
    result = runner.invoke(app, ["audit", str(folder), "--auto"])
    assert result.exit_code == 0, result.stdout
    assert "clock-drift-by-camera" in result.stdout
    # Singleton clock-drift stays quiet (multi-camera folder).
    assert "review clock-drift:" not in result.stdout


def test_clock_drift_by_camera_yes_medium_applies_delta(tmp_path: Path):
    folder = _build_two_camera_folder(tmp_path, offset_seconds=3 * 3600)
    result = runner.invoke(
        app, ["audit", str(folder), "--write", "--auto", "--yes-medium"],
    )
    assert result.exit_code == 0, result.stdout
    # A_ files untouched (they're the reference); B_ files shifted +3h.
    # B_0000 original was 07:00:00 → should now read 10:00:00.
    tags = _xmp_tags(folder / "B_0000.xmp")
    assert tags["XMP:DateTimeOriginal"] == "2026:04:01 10:00:00"
    # A_0000 gets no XMP sidecar from this rule.
    assert not (folder / "A_0000.xmp").exists()


def test_clock_drift_by_camera_batch_prompt_is_single(tmp_path: Path):
    folder = _build_two_camera_folder(tmp_path, count=5)
    # 5 Sony files would produce 5 findings; they should collapse to ONE
    # prompt with "apply to all".
    result = runner.invoke(
        app, ["audit", str(folder), "--write"],
        input="\n\ny\n",  # skip coords, skip tz, accept the single batch prompt
    )
    assert result.exit_code == 0, result.stdout
    assert "apply to all?" in result.stdout
    assert "accepted 5 file(s)" in result.stdout


def test_clock_drift_by_camera_skips_noise(tmp_path: Path):
    # 2 min offset — below MIN_DRIFT_SECONDS (5 min).
    folder = _build_two_camera_folder(tmp_path, offset_seconds=120)
    result = runner.invoke(app, ["audit", str(folder), "--auto"])
    assert result.exit_code == 0
    assert "clock-drift-by-camera" not in result.stdout


def test_clock_drift_by_camera_skips_sanity_max(tmp_path: Path):
    # 30 day offset — above MAX_DRIFT_SECONDS (14 days).
    folder = _build_two_camera_folder(tmp_path, offset_seconds=30 * 86400)
    result = runner.invoke(app, ["audit", str(folder), "--auto"])
    assert result.exit_code == 0
    assert "clock-drift-by-camera" not in result.stdout


def test_clock_drift_single_camera_unaffected(tmp_path: Path):
    # Single-camera folder with one outlier still goes through folder-median
    # clock-drift (shipped 2a.2), not clock-drift-by-camera.
    src = FIXTURES / "clock-drift-simple"
    folder = tmp_path / "clock-single"
    shutil.copytree(src, folder)
    result = runner.invoke(app, ["audit", str(folder), "--auto"])
    assert result.exit_code == 0
    assert "review clock-drift:" in result.stdout
    assert "clock-drift-by-camera" not in result.stdout


def test_notes_file_not_overwritten(dji_fixture: Path):
    existing = dji_fixture / "TRIP.md"
    existing.write_text("# Pre-existing trip notes\n")
    runner.invoke(app, ["audit", str(dji_fixture), "--auto"])
    assert existing.read_text() == "# Pre-existing trip notes\n"
    assert not (dji_fixture / "README.md").exists()
