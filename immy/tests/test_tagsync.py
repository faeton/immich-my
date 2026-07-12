"""Tests for `immy/tagsync.py` — the native Immich Tag API push that closes
the gap `trip-tags-from-notes` (XMP-only) leaves for video assets.

Rows are passed explicitly (bypassing `read_folder`'s real exiftool call),
same pattern as `test_backfill_dates.py` — these fixture files aren't real
media, only `file_camera`'s make/model + filename-prefix logic matters here.
"""

from __future__ import annotations

from pathlib import Path

from immy import tagsync
from immy.exif import ExifRow
from immy.pg import LibraryInfo

_LIB = LibraryInfo(id="lib", owner_id="owner", container_root="/originals")


class _FakeClient:
    def __init__(self):
        self.tags_upserted: list[list[str]] = []
        self.assets_tagged: list[tuple[str, list[str]]] = []

    def upsert_tags(self, names):
        self.tags_upserted.append(list(names))
        return {n: f"tag-{n}" for n in names}

    def tag_assets(self, tag_id, asset_ids):
        self.assets_tagged.append((tag_id, list(asset_ids)))
        return [{"id": aid, "success": True} for aid in asset_ids]


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
    """Resolves any `FROM asset` lookup to `id-<filename>`; None to fail it.
    `camera={asset_id: (make, model, locked)}` seeds `read_camera` results
    for pre-existing asset_exif rows; defaults to (None, None, [])."""

    def __init__(self, resolves: bool = True, camera: dict | None = None):
        self.resolves = resolves
        self.camera = camera or {}
        self.calls: list = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        row = None
        if "asset_exif" in sql:  # read_camera
            asset_id = params[0] if isinstance(params, tuple) else params
            row = self.camera.get(asset_id, (None, None, []))
        elif "FROM asset" in sql and self.resolves:
            container_path = params[-1] if isinstance(params, tuple) else params
            row = (f"id-{Path(container_path).name}",)

        class _R:
            def fetchone(_self):
                return row
        return _R()


def _trip(tmp_path: Path, *, notes: str) -> Path:
    trip = tmp_path / "2026-06-corsica-sardinia-yacht"
    trip.mkdir()
    (trip / "TRIP.md").write_text(notes)
    return trip


_NOTES = """---
tags:
- Events/2026-06-corsica-sardinia-yacht
- Gear/Camera/DJI FC8282
- Gear/Camera/Insta360
- Source/DJI
---
"""

_DJI_ROW = lambda trip: ExifRow(
    path=trip / "DJI_0001.MP4",
    raw={"EXIF:Make": "DJI", "EXIF:Model": "FC8282"})
_INSTA360_ROW = lambda trip: ExifRow(
    path=trip / "VID_20260625.insv",
    raw={"QuickTime:Make": "Insta360", "QuickTime:Model": "Insta360"})


def test_no_tags_in_notes_is_a_noop(tmp_path: Path):
    trip = _trip(tmp_path, notes="---\n---\n")
    out = tagsync.tag_sync_folder(
        _Conn(), _FakeClient(), _LIB, trip, rows=[_DJI_ROW(trip)], write=False)
    assert out == []


def test_no_notes_file_at_all_is_a_noop(tmp_path: Path):
    # Distinct from the case above: no TRIP.md/IMMY.md/README.md exists at
    # all (vs. one existing with empty front-matter).
    trip = tmp_path / "2026-06-corsica-sardinia-yacht"
    trip.mkdir()
    out = tagsync.tag_sync_folder(
        _Conn(), _FakeClient(), _LIB, trip, rows=[_DJI_ROW(trip)], write=False)
    assert out == []


def test_dry_run_reports_would_tag_without_api_calls(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _FakeClient()
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip, rows=[_DJI_ROW(trip)], write=False)
    assert len(out) == 1
    assert out[0].status == "would-tag"
    assert "Gear/Camera/DJI FC8282" in out[0].tags
    assert client.tags_upserted == [] and client.assets_tagged == []


def test_write_pushes_native_tags_per_camera(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _FakeClient()
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip,
        rows=[_DJI_ROW(trip), _INSTA360_ROW(trip)], write=True)
    assert all(o.status == "tagged" for o in out)

    pushed = {name for batch in client.tags_upserted for name in batch}
    assert "Gear/Camera/DJI FC8282" in pushed
    assert "Gear/Camera/Insta360" in pushed
    assert "Events/2026-06-corsica-sardinia-yacht" in pushed

    tagged_ids = dict(client.assets_tagged)
    assert tagged_ids["tag-Gear/Camera/DJI FC8282"] == ["id-DJI_0001.MP4"]
    assert tagged_ids["tag-Gear/Camera/Insta360"] == ["id-VID_20260625.insv"]


def test_no_asset_skips_without_failing(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _FakeClient()
    out = tagsync.tag_sync_folder(
        _Conn(resolves=False), client, _LIB, trip,
        rows=[_DJI_ROW(trip)], write=True)
    assert out[0].status == "no-asset"
    assert client.tags_upserted == [] and client.assets_tagged == []


# --- tag-failed: upsert_tags doesn't return an id for a requested name ----
# Regression coverage for the 2026-07-12 bug: `tag_sync_folder` used to mark
# every row "tagged" as soon as it *decided* what to push, before the API
# call ran — so a name mismatch in `upsert_tags`'s response (the real-world
# cause: it used to key by the API's `name` field, which is leaf-only for
# hierarchical tags) meant `tag_assets` silently never fired, yet the run
# still reported success. This class of fake models that failure mode.

class _PartiallyBrokenClient(_FakeClient):
    """Mimics the real upsert_tags bug: only returns ids for names in
    `working`; everything else is silently missing from the response, same
    as if the API's response key didn't match what the caller asked for."""

    def __init__(self, working: set[str]):
        super().__init__()
        self.working = working

    def upsert_tags(self, names):
        self.tags_upserted.append(list(names))
        return {n: f"tag-{n}" for n in names if n in self.working}


def test_tag_failed_when_upsert_omits_a_requested_name(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    # Every name resolves except the DJI gear tag this row actually needs.
    client = _PartiallyBrokenClient(working={
        "Events/2026-06-corsica-sardinia-yacht", "Source/DJI",
        "Gear/Camera/Insta360",
    })
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip, rows=[_DJI_ROW(trip)], write=True)
    assert out[0].status == "tag-failed"
    # Only the resolved tags got attached; the unresolved one never fires.
    attached_tags = {tag_id for tag_id, _ in client.assets_tagged}
    assert "tag-Gear/Camera/DJI FC8282" not in attached_tags


def test_tag_failed_does_not_mask_other_rows_success(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _PartiallyBrokenClient(working={
        "Events/2026-06-corsica-sardinia-yacht", "Source/DJI",
        "Gear/Camera/DJI FC8282",
    })  # Insta360 gear tag is the one that fails to resolve.
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip,
        rows=[_DJI_ROW(trip), _INSTA360_ROW(trip)], write=True)
    by_file = {o.media.name: o.status for o in out}
    assert by_file["DJI_0001.MP4"] == "tagged"
    assert by_file["VID_20260625.insv"] == "tag-failed"


def test_total_upsert_failure_marks_all_rows_tag_failed(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _PartiallyBrokenClient(working=set())  # nothing resolves
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip, rows=[_DJI_ROW(trip)], write=True)
    assert out[0].status == "tag-failed"
    assert client.assets_tagged == []  # tag_assets never called for anything


# --- tag_assets() itself reporting a genuine per-asset failure ------------
# The id-resolution fix above doesn't catch a failure at the SECOND API call
# — `tag_assets` can resolve a tag id fine and still fail to attach it to a
# given asset. `success=False, error="duplicate"` is expected/idempotent
# (already attached) and must NOT count as a failure; anything else must.

class _AttachFailingClient(_FakeClient):
    """`tag_assets` reports failure for asset ids in `failing_asset_ids`,
    with `error`. Everything else succeeds normally."""

    def __init__(self, failing_asset_ids: set[str], error: str = "server-error"):
        super().__init__()
        self.failing_asset_ids = failing_asset_ids
        self.error = error

    def tag_assets(self, tag_id, asset_ids):
        self.assets_tagged.append((tag_id, list(asset_ids)))
        return [
            {"id": aid, "success": False, "error": self.error}
            if aid in self.failing_asset_ids
            else {"id": aid, "success": True}
            for aid in asset_ids
        ]


def test_tag_assets_genuine_failure_marks_row_tag_failed(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _AttachFailingClient(failing_asset_ids={"id-DJI_0001.MP4"})
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip, rows=[_DJI_ROW(trip)], write=True)
    assert out[0].status == "tag-failed"


def test_tag_assets_duplicate_is_not_a_failure(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _AttachFailingClient(
        failing_asset_ids={"id-DJI_0001.MP4"}, error="duplicate")
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip, rows=[_DJI_ROW(trip)], write=True)
    assert out[0].status == "tagged"


class _UnattributableFailureClient(_FakeClient):
    """`tag_assets` reports a failure with no `id` at all — can't tell which
    asset it belongs to. Every asset requested for that tag must fail
    conservatively rather than default to success."""

    def tag_assets(self, tag_id, asset_ids):
        self.assets_tagged.append((tag_id, list(asset_ids)))
        return [{"success": False, "error": "server-error"}]


def test_tag_assets_failure_without_id_fails_whole_batch(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    client = _UnattributableFailureClient()
    out = tagsync.tag_sync_folder(
        _Conn(), client, _LIB, trip,
        rows=[_DJI_ROW(trip), _INSTA360_ROW(trip)], write=True)
    assert all(o.status == "tag-failed" for o in out)


# --- camera_sync_folder: backfill asset_exif.make/model via devices.resolve
# (never a raw module code), falling back to the notes gear tag — itself run
# through devices.resolve — only when the file has no signal at all --------

# A DJI still: real EXIF Model is the bare module code, exactly like a real
# DJI JPG (confirmed live 2026-07-12: DJI video carries neither Make/Model
# nor an Encoder atom, but stills do).
_DJI_STILL_ROW = lambda trip: ExifRow(
    path=trip / "DJI_0001.JPG", raw={"EXIF:Make": "DJI", "EXIF:Model": "FC8482"})

# A DJI video: no EXIF/QuickTime Make/Model/Encoder at all — the case that
# forces the notes-gear-tag fallback.
_DJI_VIDEO_NO_SIGNAL_ROW = lambda trip: ExifRow(
    path=trip / "DJI_0002.MP4", raw={})

# A DJI video that DOES carry a real device Encoder atom.
_DJI_VIDEO_ENCODER_ROW = lambda trip: ExifRow(
    path=trip / "DJI_0003.MP4", raw={"ItemList:Encoder": "DJIMavic3Cine"})

_NON_DJI_ROW = lambda trip: ExifRow(
    path=trip / "IMG_0001.HEIC",
    raw={"EXIF:Make": "Apple", "EXIF:Model": "iPhone 17 Pro"})


def test_camera_still_resolves_friendly_name_without_notes(tmp_path: Path):
    # No notes needed at all — real EXIF Model is enough.
    trip = tmp_path / "no-notes-trip"
    trip.mkdir()
    out = tagsync.camera_sync_folder(
        _Conn(), _LIB, trip, rows=[_DJI_STILL_ROW(trip)], write=True)
    assert out[0].status == "written"
    assert (out[0].make, out[0].model) == ("DJI", "Mini 4 Pro")


def test_camera_non_dji_camera_passes_through_unchanged(tmp_path: Path):
    trip = tmp_path / "no-notes-trip"
    trip.mkdir()
    out = tagsync.camera_sync_folder(
        _Conn(), _LIB, trip, rows=[_NON_DJI_ROW(trip)], write=True)
    assert out[0].status == "written"
    assert (out[0].make, out[0].model) == ("Apple", "iPhone 17 Pro")


def test_camera_video_encoder_atom_resolves_friendly_name(tmp_path: Path):
    trip = tmp_path / "no-notes-trip"
    trip.mkdir()
    out = tagsync.camera_sync_folder(
        _Conn(), _LIB, trip, rows=[_DJI_VIDEO_ENCODER_ROW(trip)], write=True)
    assert out[0].status == "written"
    assert (out[0].make, out[0].model) == ("DJI", "Mavic 3 Cine")


def test_camera_video_no_file_signal_falls_back_to_notes_gear_tag(tmp_path: Path):
    # No EXIF/Encoder signal at all — must fall back to the notes gear tag,
    # AND resolve its module code through devices.resolve (friendly name,
    # not the raw "FC8282").
    trip = _trip(tmp_path, notes=_NOTES)
    out = tagsync.camera_sync_folder(
        _Conn(), _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=True)
    assert out[0].status == "written"
    assert (out[0].make, out[0].model) == ("DJI", "Air 3")


def test_camera_dry_run_no_write(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    conn = _Conn()
    out = tagsync.camera_sync_folder(
        conn, _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=False)
    assert out[0].status == "would-write"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_camera_skips_asset_immich_already_extracted(tmp_path: Path):
    # Unlocked existing value = Immich's own extraction of GENUINELY good
    # data (not in devices.py's table, so resolve() is a no-op on it) —
    # never touch it, even though our own resolution path would produce a
    # different value from the notes gear tag.
    trip = _trip(tmp_path, notes=_NOTES)
    conn = _Conn(camera={"id-DJI_0002.MP4": ("Some", "Other Camera", [])})
    out = tagsync.camera_sync_folder(
        conn, _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=True)
    assert out[0].status == "skip-has-camera"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_camera_upgrades_preexisting_unlocked_raw_code(tmp_path: Path):
    # Regression for the 2026-07-12 discovery: an asset processed BEFORE
    # devices.py's friendly-name table existed carries a raw module code,
    # unlocked, from Immich's/an older immy's own extraction. Since that
    # exact code IS in our confirmed table, upgrading it is a confident
    # lookup, not a guess — must correct it (and lock it going forward),
    # unlike the genuinely-unknown case above.
    trip = _trip(tmp_path, notes=_NOTES)
    conn = _Conn(camera={"id-DJI_0002.MP4": ("DJI", "FC8482", [])})
    out = tagsync.camera_sync_folder(
        conn, _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=True)
    assert out[0].status == "written"
    assert (out[0].make, out[0].model) == ("DJI", "Mini 4 Pro")
    update = [c for c in conn.calls if c[0].lstrip().upper().startswith("UPDATE")]
    assert update and update[0][1]["model"] == "Mini 4 Pro"
    assert "lockedProperties" in update[0][0]


def test_camera_upgrade_dry_run_reports_without_writing(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    conn = _Conn(camera={"id-DJI_0002.MP4": ("DJI", "FC8482", [])})
    out = tagsync.camera_sync_folder(
        conn, _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=False)
    assert out[0].status == "would-write"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_camera_already_correct_and_locked_is_a_noop(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    conn = _Conn(camera={
        "id-DJI_0002.MP4": ("DJI", "Air 3", ["make", "model"])})
    out = tagsync.camera_sync_folder(
        conn, _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=True)
    assert out[0].status == "skip-has-camera"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_camera_corrects_our_own_stale_locked_value(tmp_path: Path):
    # Regression: a prior run of this command wrote a raw code (the actual
    # 2026-07-12 bug) or a since-superseded value. It's locked by US
    # (lockedProperties has make+model), so re-running must correct it —
    # unlike the Immich-owned case above.
    trip = _trip(tmp_path, notes=_NOTES)
    conn = _Conn(camera={
        "id-DJI_0002.MP4": ("DJI", "FC8282", ["make", "model"])})
    out = tagsync.camera_sync_folder(
        conn, _LIB, trip, rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=True)
    assert out[0].status == "corrected"
    assert (out[0].make, out[0].model) == ("DJI", "Air 3")
    update = [c for c in conn.calls if c[0].lstrip().upper().startswith("UPDATE")]
    assert update and update[0][1]["model"] == "Air 3"


def test_camera_no_signal_anywhere(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    row = ExifRow(path=trip / "random.mp4", raw={})  # no camera signal at all
    out = tagsync.camera_sync_folder(
        conn := _Conn(), _LIB, trip, rows=[row], write=True)
    assert out[0].status == "no-signal"
    assert not any(c[0].lstrip().upper().startswith("UPDATE") for c in conn.calls)


def test_camera_no_asset_skips(tmp_path: Path):
    trip = _trip(tmp_path, notes=_NOTES)
    out = tagsync.camera_sync_folder(
        _Conn(resolves=False), _LIB, trip,
        rows=[_DJI_VIDEO_NO_SIGNAL_ROW(trip)], write=True)
    assert out[0].status == "no-asset"
