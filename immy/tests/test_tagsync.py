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


class _Conn:
    """Resolves any `FROM asset` lookup to `id-<filename>`; None to fail it."""

    def __init__(self, resolves: bool = True):
        self.resolves = resolves

    def execute(self, sql, params=None):
        row = None
        if "FROM asset" in sql and self.resolves:
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
