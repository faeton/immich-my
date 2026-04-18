from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immy.cli import app

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


def test_audit_empty_folder_exits_zero(tmp_path):
    result = runner.invoke(app, ["audit", str(tmp_path)])
    assert result.exit_code == 0
    assert "0 media file" in result.stdout


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
    assert tags["XMP:DateTimeOriginal"] == "2026:03:05 09:49:01"

    state = dji_fixture / ".audit" / "state.yml"
    assert state.is_file()
    log = dji_fixture / ".audit" / "audit.jsonl"
    assert log.is_file()
    # Three rules fire: dji-gps-from-srt, dji-date-from-srt, trip-tags-from-notes
    # (ensure_notes scaffolded README.md with auto-suggested tags).
    assert len(log.read_text().splitlines()) == 3


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
    import yaml
    _seed_clock_drift_with_coords(clock_drift_fixture)
    # Tz prompt fires (EXIF dates are naive); pipe zone + 'n' to skip MEDIUM.
    result = runner.invoke(
        app,
        ["audit", str(clock_drift_fixture), "--write"],
        input="Indian/Mauritius\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    fm = yaml.safe_load(
        (clock_drift_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert fm["timezone"] == "Indian/Mauritius"
    # Cascade: trip-timezone HIGH rule rewrites XMP dates with +04:00.
    tags = _xmp_tags(clock_drift_fixture / "DSC_0001.xmp")
    assert tags["XMP:DateTimeOriginal"].endswith("+04:00")


def test_interactive_tz_prompt_rejects_unknown_zone(clock_drift_fixture: Path):
    import yaml
    _seed_clock_drift_with_coords(clock_drift_fixture)
    result = runner.invoke(
        app,
        ["audit", str(clock_drift_fixture), "--write"],
        input="Not/A/Real/Zone\nn\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "unknown zone" in result.stdout
    fm = yaml.safe_load(
        (clock_drift_fixture / "TRIP.md").read_text().split("---", 2)[1]
    )
    assert fm.get("timezone") in (None, "")


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


def test_notes_file_not_overwritten(dji_fixture: Path):
    existing = dji_fixture / "TRIP.md"
    existing.write_text("# Pre-existing trip notes\n")
    runner.invoke(app, ["audit", str(dji_fixture), "--auto"])
    assert existing.read_text() == "# Pre-existing trip notes\n"
    assert not (dji_fixture / "README.md").exists()
