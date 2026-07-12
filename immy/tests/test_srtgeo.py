"""Tests for `immy/srtgeo.py` — caption context, GPS write SQL, and the
geotag decision logic (DB faked; the live verify-channel probe is run
manually against the NAS)."""

from __future__ import annotations

from pathlib import Path

from immy import srtgeo
from immy.exif import ExifRow
from immy.pg import LibraryInfo

FIXTURES = Path(__file__).parent / "fixtures" / "dji-srt-pair"


# --- caption context ------------------------------------------------------

def test_caption_context_for_drone_clip(tmp_path: Path):
    media = tmp_path / "DJI_MULTI.MP4"
    media.write_bytes(b"")
    (tmp_path / "DJI_MULTI.SRT").write_text((FIXTURES / "DJI_MULTI.SRT").read_text())
    ctx = srtgeo.caption_context_for(media, tmp_path, reverse=False)
    # rel_alt of the takeoff fix (12 m), no notes place, reverse off.
    assert ctx == "aerial drone shot, ~12 m above ground."


def test_caption_context_for_non_drone_is_none(tmp_path: Path):
    media = tmp_path / "IMG_1.JPG"
    media.write_bytes(b"")
    assert srtgeo.caption_context_for(media, tmp_path, reverse=False) is None


def test_caption_context_includes_notes_place(tmp_path: Path):
    media = tmp_path / "DJI_MULTI.MP4"
    media.write_bytes(b"")
    (tmp_path / "DJI_MULTI.SRT").write_text((FIXTURES / "DJI_MULTI.SRT").read_text())
    (tmp_path / "TRIP.md").write_text(
        "---\nlocation:\n  name: Barcelona, Spain\n---\n"
    )
    ctx = srtgeo.caption_context_for(media, tmp_path, reverse=False)
    assert ctx == "aerial drone shot, ~12 m above ground, near Barcelona, Spain."


# --- GPS write SQL --------------------------------------------------------

class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        if sql.lstrip().upper().startswith("UPDATE"):
            self.rowcount = 1
        return self


class _Conn:
    """Minimal psycopg stand-in routing SELECTs by table name."""

    def __init__(self, *, asset_id=None, gps=(None, None), locked=None):
        self.asset_id = asset_id
        self.gps = gps
        self.locked = locked or []
        self.calls = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        row = None
        if "asset_exif" in sql:  # read_gps
            row = (self.gps[0], self.gps[1], self.locked)
        elif "FROM asset" in sql:  # resolve_asset_id
            row = (self.asset_id,) if self.asset_id else None

        class _R:
            def fetchone(_self):
                return row
        return _R()


def test_write_gps_locked_passes_tokens():
    conn = _Conn()
    n = srtgeo.write_gps(conn, "aid", 1.0, 2.0, lock=True,
                         lock_tokens=("latitude", "longitude"))
    assert n == 1
    sql, params = conn.calls[-1]
    assert "lockedProperties" in sql
    assert params["lat"] == 1.0 and params["lon"] == 2.0
    assert params["lock_tokens"] == ["latitude", "longitude"]


def test_write_gps_unlocked_omits_lock():
    conn = _Conn()
    srtgeo.write_gps(conn, "aid", 1.0, 2.0, lock=False)
    sql, params = conn.calls[-1]
    assert "lockedProperties" not in sql
    assert "lock_tokens" not in params


# --- geotag decision logic ------------------------------------------------

def _drone_row(tmp_path: Path) -> ExifRow:
    media = tmp_path / "DJI_MULTI.MP4"
    media.write_bytes(b"")
    (tmp_path / "DJI_MULTI.SRT").write_text((FIXTURES / "DJI_MULTI.SRT").read_text())
    return ExifRow(path=media, raw={})


_LIB = LibraryInfo(id="lib", owner_id="owner", container_root="/originals")

_NULL_ISLAND_SRT = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2024-08-12 18:30:00.000
[iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [focal_len: 24.00] [latitude: 0.0] [longitude: 0.0] [rel_alt: 0.000 abs_alt: 0.000] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">FrameCnt: 2, DiffTime: 33ms
2024-08-12 18:30:01.000
[iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [focal_len: 24.00] [latitude: 0.0] [longitude: 0.0] [rel_alt: 0.000 abs_alt: 0.000] </font>
"""


def _no_fix_row(tmp_path: Path) -> ExifRow:
    """A drone clip whose .SRT never gets a satellite lock — every frame is
    the (0,0) null-island sentinel `first_valid_fix` is built to skip."""
    media = tmp_path / "DJI_NOFIX.MP4"
    media.write_bytes(b"")
    (tmp_path / "DJI_NOFIX.SRT").write_text(_NULL_ISLAND_SRT)
    return ExifRow(path=media, raw={})


def test_geotag_would_tag_dry_run(tmp_path: Path):
    conn = _Conn(asset_id="aid", gps=(None, None))
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(conn, _LIB, tmp_path, rows, write=False)
    assert len(out) == 1
    o = out[0]
    assert o.status == "would-tag"
    assert o.asset_id == "aid"
    assert (o.lat, o.lon) == (41.385100, 2.173400)  # takeoff fix
    # No UPDATE issued in a dry run.
    assert not any("UPDATE" in c[0].upper() for c in conn.calls)


def test_geotag_writes_when_apply(tmp_path: Path):
    conn = _Conn(asset_id="aid", gps=(None, None))
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(conn, _LIB, tmp_path, rows, write=True)
    assert out[0].status == "tagged"
    update = [c for c in conn.calls if c[0].lstrip().upper().startswith("UPDATE")]
    assert update and update[0][1]["lat"] == 41.385100


def test_geotag_skips_existing_db_gps(tmp_path: Path):
    conn = _Conn(asset_id="aid", gps=(10.0, 20.0))  # already located
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(conn, _LIB, tmp_path, rows, write=True)
    assert out[0].status == "skip-has-gps"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_geotag_ignores_file_gps_when_db_null(tmp_path: Path):
    # The bug fix: a drone clip whose GPS lives only in the file (an `immy
    # audit` dji-gps-from-srt XMP sidecar merged into the row, or an embedded
    # container tag) but is NULL in the DB STILL needs tagging — Immich never
    # ingests video XMP. The file-level GPS must NOT short-circuit; the DB
    # decides. Here DB gps is (None, None), so we tag.
    media = tmp_path / "DJI_MULTI.MP4"
    media.write_bytes(b"")
    (tmp_path / "DJI_MULTI.SRT").write_text((FIXTURES / "DJI_MULTI.SRT").read_text())
    row = ExifRow(path=media, raw={
        "XMP:GPSLatitude": "1.0", "XMP:GPSLongitude": "2.0",
    })
    conn = _Conn(asset_id="aid", gps=(None, None))
    out = srtgeo.geotag_folder(conn, _LIB, tmp_path, [row], write=True)
    assert out[0].status == "tagged"


# --- --relock: repair unlocked-but-present coords -------------------------

def test_geotag_relock_off_by_default_leaves_unlocked_gps_alone(tmp_path: Path):
    # Same DB state a --relock run would repair, but relock=False (default):
    # must behave exactly like the pre-existing skip-has-gps path.
    conn = _Conn(asset_id="aid", gps=(41.385100, 2.173400), locked=[])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(conn, _LIB, tmp_path, rows, write=True)
    assert out[0].status == "skip-has-gps"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_geotag_relock_dry_run_matching_coord(tmp_path: Path):
    conn = _Conn(asset_id="aid", gps=(41.385100, 2.173400), locked=[])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=False, relock=True)
    assert out[0].status == "would-relock"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_geotag_relock_writes_and_locks_matching_coord(tmp_path: Path):
    conn = _Conn(asset_id="aid", gps=(41.385100, 2.173400), locked=[])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=True, relock=True)
    assert out[0].status == "relocked"
    update = [c for c in conn.calls if c[0].lstrip().upper().startswith("UPDATE")
              and "latitude" in c[0]]
    assert update and "lockedProperties" in update[0][0]
    assert update[0][1]["lat"] == 41.385100 and update[0][1]["lon"] == 2.173400


def test_geotag_relock_skips_coord_far_from_srt_fix(tmp_path: Path):
    # A DB coord nowhere near the SRT fix is presumptively a location you
    # pinned by hand in the app — --relock must not touch it.
    conn = _Conn(asset_id="aid", gps=(0.0, 0.0), locked=[])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=True, relock=True)
    assert out[0].status == "skip-mismatch"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_geotag_relock_skips_already_locked(tmp_path: Path):
    # Already durable — nothing to repair, even with --relock.
    conn = _Conn(
        asset_id="aid", gps=(41.385100, 2.173400),
        locked=["latitude", "longitude"])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=True, relock=True)
    assert out[0].status == "skip-has-gps"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_geotag_relock_just_inside_tolerance_boundary(tmp_path: Path):
    # 1900m from the SRT fix — inside the 2000m tolerance.
    conn = _Conn(asset_id="aid", gps=(41.40218711051246, 2.173400), locked=[])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=True, relock=True)
    assert out[0].status == "relocked"


def test_geotag_relock_just_outside_tolerance_boundary(tmp_path: Path):
    # 2100m from the SRT fix — outside the 2000m tolerance.
    conn = _Conn(asset_id="aid", gps=(41.4039857537243, 2.173400), locked=[])
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=True, relock=True)
    assert out[0].status == "skip-mismatch"


def test_geotag_no_fix_with_relock_and_existing_gps(tmp_path: Path):
    # A .SRT that never got a satellite lock: relock has nothing to compare
    # the existing DB coord against, so it must not touch it either way.
    conn = _Conn(asset_id="aid", gps=(10.0, 20.0), locked=[])
    rows = [_no_fix_row(tmp_path)]
    out = srtgeo.geotag_folder(
        conn, _LIB, tmp_path, rows, write=True, relock=True)
    assert out[0].status == "no-fix"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_geotag_no_asset(tmp_path: Path):
    conn = _Conn(asset_id=None, gps=(None, None))
    rows = [_drone_row(tmp_path)]
    out = srtgeo.geotag_folder(conn, _LIB, tmp_path, rows, write=True)
    assert out[0].status == "no-asset"


def test_is_uuid():
    assert srtgeo.is_uuid("dc68c016-1eba-4335-8638-a52596470ed2")
    assert not srtgeo.is_uuid("DJI_0001.MP4")


# --- reverse geocode (vendored map + Immich-style query) ------------------

def test_country_name_vendored_map():
    from immy import geocode
    assert geocode.country_name("PE") == "Peru"
    assert geocode.country_name("BO") == "Bolivia"          # list → first
    assert geocode.country_name("BOL") == "Bolivia"         # alpha-3
    assert geocode.country_name("US") == "United States of America"
    assert geocode.country_name(None) is None
    assert geocode.country_name("ZZ") is None


class _GeoConn:
    """Fake conn for geocode: routes the nearest/fallback SELECTs."""

    def __init__(self, nearest=None, fallback=None):
        self.nearest = nearest      # (name, admin1, countryCode)
        self.fallback = fallback    # (admin_a3,)

    def execute(self, sql, params=None):
        row = self.nearest if "geodata_places" in sql else (
            self.fallback if "naturalearth_countries" in sql else None)

        class _R:
            def fetchone(_s):
                return row
        return _R()


def test_reverse_geocode_nearest():
    from immy import geocode
    p = geocode.reverse_geocode(
        _GeoConn(nearest=("Machupicchu", "Cuzco Department", "PE")), -13.16, -72.5)
    assert (p.city, p.state, p.country) == ("Machupicchu", "Cuzco Department", "Peru")


def test_reverse_geocode_fallback_country_only():
    from immy import geocode
    p = geocode.reverse_geocode(_GeoConn(nearest=None, fallback=("BOL",)), 0, 0)
    assert (p.city, p.state, p.country) == (None, None, "Bolivia")


def test_reverse_geocode_empty():
    from immy import geocode
    assert geocode.reverse_geocode(_GeoConn(), 0, 0).is_empty()


def test_geotag_writes_place(tmp_path: Path, monkeypatch):
    from immy import geocode
    monkeypatch.setattr(
        geocode, "reverse_geocode",
        lambda conn, lat, lon, **k: geocode.Place(
            country="Peru", state="Cuzco Department", city="Machupicchu"))
    conn = _Conn(asset_id="aid", gps=(None, None))
    rows = [_drone_row(tmp_path)]
    srtgeo.geotag_folder(conn, _LIB, tmp_path, rows, write=True)
    place_upd = [c for c in conn.calls
                 if c[0].lstrip().upper().startswith("UPDATE") and "country" in c[0]]
    assert place_upd and place_upd[0][1]["country"] == "Peru"
    assert place_upd[0][1]["city"] == "Machupicchu"
