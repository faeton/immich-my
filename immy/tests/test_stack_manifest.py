"""`immy stacks apply` — manifest parsing, planning, write path, CLI."""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from immy import cli, stack_manifest as sm
from immy.immich import ImmichError

runner = CliRunner()

FOLDER = "2026-tesla-dashcam"
EVENT = "2026-09-09_19-16-35__granada__honk"


def _manifest(**overrides):
    group = {
        "key": "2026-09-09_19-16-35",
        "primary": f"{EVENT}/mosaic.mp4",
        "children": [f"{EVENT}/front.mp4", f"{EVENT}/back.mp4"],
        "tags": ["Source/Tesla", "Tesla/Reason/honk"],
        "archive": False,
    }
    group.update(overrides)
    return {"schema": 1, "album": "Tesla Dashcam", "groups": [group]}


# --- parsing ----------------------------------------------------------------

def test_parse_manifest_round_trip():
    m = sm.parse_manifest(_manifest())
    assert m.album == "Tesla Dashcam"
    (g,) = m.groups
    assert g.primary == f"{EVENT}/mosaic.mp4"
    assert g.children == (f"{EVENT}/front.mp4", f"{EVENT}/back.mp4")
    assert g.tags == ("Source/Tesla", "Tesla/Reason/honk")
    assert g.archive is False
    assert g.paths == (g.primary, *g.children)


@pytest.mark.parametrize("bad, msg", [
    ({"schema": 2, "groups": []}, "unsupported schema"),
    ({"schema": 1, "groups": [], "albumn": "x"}, "unknown manifest keys"),
    (_manifest(childs=[]), "unknown keys"),
    (_manifest(primary="../escape/mosaic.mp4"), "'..'"),
    (_manifest(children=[f"{EVENT}/../front.mp4"]), "'..'"),
    (_manifest(primary="/abs/mosaic.mp4"), "not a relative"),
    (_manifest(primary=""), "non-empty"),
    (_manifest(children=[f"{EVENT}/mosaic.mp4"]), "listed twice"),
    (_manifest(tags="Source/Tesla"), "list of non-empty strings"),
    (_manifest(tags=["/Source"]), "bad tag path"),
    (_manifest(archive="yes"), "true/false"),
    (_manifest(key=""), "key must be"),
    ([], "JSON object"),
])
def test_parse_manifest_rejects(bad, msg):
    with pytest.raises(sm.ManifestError, match=msg):
        sm.parse_manifest(bad)


def test_parse_manifest_rejects_a_path_in_two_groups():
    data = _manifest()
    second = dict(data["groups"][0], key="other", primary=f"{EVENT}/x.mp4",
                  children=[f"{EVENT}/front.mp4"])
    data["groups"].append(second)
    with pytest.raises(sm.ManifestError, match="already belongs to group"):
        sm.parse_manifest(data)


def test_load_manifest_errors(tmp_path: Path):
    with pytest.raises(sm.ManifestError, match="no manifest"):
        sm.load_manifest(tmp_path / "stacks.json")
    (tmp_path / "stacks.json").write_text("{nope")
    with pytest.raises(sm.ManifestError, match="invalid JSON"):
        sm.load_manifest(tmp_path / "stacks.json")


# --- read -------------------------------------------------------------------

class _Conn:
    """Answers the folder query with canned rows; records SQL + tag links."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[tuple[str, object]] = []
        self.links: list[tuple[str, str, str]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last_sql = sql
        return self

    def fetchall(self):
        # the folder query answers with the canned asset rows; the stack
        # membership query (`"stackId" = ANY`) with (stack id, asset id) pairs
        if "= ANY(" in getattr(self, "_last_sql", ""):
            return [r for r in self.rows if len(r) == 2]
        return [r for r in self.rows if len(r) != 2]

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, params):
        if "INSERT INTO tag_asset" in sql:
            for p in params:
                self.conn.links.append((p["asset"], p["tag"], p["value"]))


def _row(name, asset_id, stack_id=None, primary_id=None, size=0, event=EVENT):
    return (asset_id, f"/originals/{FOLDER}/{event}/{name}", stack_id, primary_id, size)


def test_fetch_folder_assets_keys_by_relative_path_and_escapes_like():
    conn = _Conn([
        _row("mosaic.mp4", "a1", "s1", "a1", 2),
        _row("front.mp4", "a2", "s1", "a1", 2),
        ("a3", f"/originals/{FOLDER}/nested/{FOLDER}/clip.mp4", None, None, 0),
    ])
    assets = sm.fetch_folder_assets(conn, "2026_tesla%")
    ((sql, params),) = conn.calls
    assert "LIKE %s ESCAPE" in sql and '"deletedAt" IS NULL' in sql
    assert params == (r"%/2026\_tesla\%/%",)
    assets = sm.fetch_folder_assets(conn, FOLDER)
    (m,) = assets[f"{EVENT}/mosaic.mp4"]
    assert (m.id, m.stack_id, m.stack_primary_id, m.stack_size) == ("a1", "s1", "a1", 2)
    # the recurring folder name resolves under BOTH candidate relative paths
    assert [a.id for a in assets["clip.mp4"]] == ["a3"]
    assert [a.id for a in assets[f"nested/{FOLDER}/clip.mp4"]] == ["a3"]


# --- plan -------------------------------------------------------------------

def _assets(rows):
    return sm.fetch_folder_assets(_Conn(rows), FOLDER)


def test_plan_create_when_nothing_stacked():
    m = sm.parse_manifest(_manifest())
    (plan,) = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ]))
    assert plan.status == "create"
    assert plan.primary.id == "a1" and [c.id for c in plan.children] == ["a2", "a3"]
    assert plan.links == 3 * 2 and plan.resolved


def test_plan_single_file_group_is_tags_only():
    """A kept event whose only ticked camera had no footage in the window ships
    just the mosaic: no stack (Immich wants two members), tags still linked."""
    m = sm.parse_manifest(_manifest(children=[]))
    (plan,) = sm.plan_groups(m, _assets([_row("mosaic.mp4", "a1")]))
    assert plan.status == "ok" and plan.note == "single file: tags only"
    assert plan.resolved and plan.links == 2 and plan.children == []


def test_plan_ok_when_stack_matches_exactly():
    m = sm.parse_manifest(_manifest())
    (plan,) = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1", "s1", "a1", 3),
        _row("front.mp4", "a2", "s1", "a1", 3),
        _row("back.mp4", "a3", "s1", "a1", 3),
    ]))
    assert plan.status == "ok" and plan.stale_stacks == []


@pytest.mark.parametrize("rows, note, restackable", [
    # other primary
    ([_row("mosaic.mp4", "a1", "s1", "a2", 3), _row("front.mp4", "a2", "s1", "a2", 3),
      _row("back.mp4", "a3", "s1", "a2", 3)], "other primary", True),
    # partial: back.mp4 was added after the first stacking
    ([_row("mosaic.mp4", "a1", "s1", "a1", 2), _row("front.mp4", "a2", "s1", "a1", 2),
      _row("back.mp4", "a3")], "some members unstacked", True),
    # stack reaches beyond the group (a 4th asset elsewhere): never torn down
    ([_row("mosaic.mp4", "a1", "s1", "a1", 4), _row("front.mp4", "a2", "s1", "a1", 4),
      _row("back.mp4", "a3", "s1", "a1", 4)], "stack has 4 members, manifest 3", False),
    # two stacks, both fully inside the group
    ([_row("mosaic.mp4", "a1", "s1", "a1", 2), _row("front.mp4", "a2", "s1", "a1", 2),
      _row("back.mp4", "a3", "s2", "a3", 1)], "members span 2 stacks", True),
])
def test_plan_differs_and_restack(rows, note, restackable):
    m = sm.parse_manifest(_manifest())
    (plan,) = sm.plan_groups(m, _assets(rows))
    assert plan.status == "differs" and plan.note == note
    (plan,) = sm.plan_groups(m, _assets(rows), restack=True)
    if restackable:
        assert plan.status == "restack"
        assert plan.stale_stacks == sorted({r[2] for r in rows if r[2]})
    else:
        # a stack with members outside the group is someone else's grouping
        assert plan.status == "differs" and "outside the group" in plan.note


def test_plan_child_replacement_is_id_changed():
    """Only the primary id used to be compared; a re-rendered child that came
    back as a new asset must be flagged the same way (count unchanged)."""
    m = sm.parse_manifest(_manifest())
    receipt = {"2026-09-09_19-16-35": sm.Receipt("a1", ("a2", "OLD3"), "s1", "2026-10-01T00:00:00+00:00")}
    (plan,) = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1", "s1", "a1", 2), _row("front.mp4", "a2", "s1", "a1", 2), _row("back.mp4", "a3"),
    ]), receipt=receipt)
    assert plan.status == "id-changed" and "child asset id" in plan.note
    # a different camera COUNT is a membership edit, not a replacement
    m2 = sm.parse_manifest(_manifest(children=[f"{EVENT}/front.mp4"]))
    (plan,) = sm.plan_groups(m2, _assets([
        _row("mosaic.mp4", "a1", "s1", "a1", 3), _row("front.mp4", "a2", "s1", "a1", 3),
    ]), receipt=receipt)
    assert plan.status == "differs"


def test_plan_single_file_still_stacked_differs():
    """All cameras un-ticked later: the mosaic still sits in the old stack with
    the (now unlisted) children — reported, torn down only with --restack and
    only when the stack holds nothing but files of this group."""
    m = sm.parse_manifest(_manifest(children=[]))
    (plan,) = sm.plan_groups(m, _assets([_row("mosaic.mp4", "a1", "s1", "a1", 3)]))
    assert plan.status == "differs" and plan.note == "single file: still in a stack of 3"
    (plan,) = sm.plan_groups(m, _assets([_row("mosaic.mp4", "a1", "s1", "a1", 3)]), restack=True)
    assert plan.status == "differs"   # the other 2 members are outside the group


def test_apply_deletes_a_shared_stale_stack_once():
    g1 = {**_manifest()["groups"][0], "key": "k1", "children": [f"{EVENT}/front.mp4"]}
    g2 = {**_manifest()["groups"][0], "key": "k2", "primary": f"{EVENT}/back.mp4", "children": [f"{EVENT}/x.mp4"]}
    m = sm.parse_manifest({"schema": 1, "album": None, "groups": [g1, g2]})
    rows = [_row("mosaic.mp4", "a1", "s1", "a1", 4), _row("front.mp4", "a2", "s1", "a1", 4),
            _row("back.mp4", "a3", "s1", "a1", 4), _row("x.mp4", "a4", "s1", "a1", 4)]
    plans = sm.plan_groups(m, _assets(rows), restack=True)
    # s1 holds exactly the four files of the two groups: each group sees two
    # of them, so from either group's view the stack reaches outside it
    assert [p.status for p in plans] == ["differs", "differs"]
    # the same with both stacks fully inside their groups is restacked, and a
    # stale stack shared by two groups is deleted once
    plans = sm.plan_groups(m, _assets(rows), restack=True)
    for p in plans:
        p.status, p.stale_stacks = "restack", ["s1"]
    client, conn = _Client(), _Conn()
    sm.apply_plans(client, conn, plans, log=lambda m: None)
    assert [c for c in client.calls if c[0] == "delete"] == [("delete", "s1")]


def test_restack_decides_by_live_membership_not_counts():
    """Codex round 2: a count of superseded receipt ids could excuse two
    strangers. With the stacks' live members known the check is exact."""
    m = sm.parse_manifest(_manifest())
    receipt = {"2026-09-09_19-16-35": sm.Receipt("OLD1", ("OLD2", "a3"), "s1", "2026-10-01T00:00:00+00:00")}
    rows = [_row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3", "s1", "OLD1", 3)]
    # s1 = {a3, OLD1, OLD2}: everything is ours or superseded → restack allowed
    (plan,) = sm.plan_groups(m, _assets(rows), receipt=receipt, restack=True,
                             stack_members={"s1": {"a3", "OLD1", "OLD2"}})
    assert plan.status == "restack" and plan.stale_stacks == ["s1"]
    # s1 = {a3, OLD1, STRANGER}: same size, but one member is nobody's → refused
    (plan,) = sm.plan_groups(m, _assets(rows), receipt=receipt, restack=True,
                             stack_members={"s1": {"a3", "OLD1", "STRANGER"}})
    assert plan.status == "id-changed" and "outside the group" in plan.note
    # the receipt's old stack with no current member in it: dissolved only
    # when it holds nothing but superseded assets
    rows2 = [_row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3")]
    (plan,) = sm.plan_groups(m, _assets(rows2), receipt=receipt, restack=True,
                             stack_members={"s1": {"OLD1", "OLD2"}})
    assert plan.status == "restack" and plan.stale_stacks == ["s1"]
    (plan,) = sm.plan_groups(m, _assets(rows2), receipt=receipt, restack=True,
                             stack_members={"s1": {"OLD1", "SOMEONE"}})
    assert plan.status == "id-changed"
    # without membership (no conn): any member beyond the group's own count is foreign
    (plan,) = sm.plan_groups(m, _assets(rows), receipt=receipt, restack=True)
    assert plan.status == "id-changed"


def test_single_file_restack_dissolves_without_creating():
    """All cameras un-ticked after a first stacking: --restack removes the
    old stack (it holds only this group's superseded children) and must NOT
    post a one-asset stack."""
    m = sm.parse_manifest(_manifest(children=[]))
    receipt = {"2026-09-09_19-16-35": sm.Receipt("a1", ("a2", "a3"), "s1", "2026-10-01T00:00:00+00:00")}
    rows = [_row("mosaic.mp4", "a1", "s1", "a1", 3)]
    (plan,) = sm.plan_groups(m, _assets(rows), receipt=receipt, restack=True,
                             stack_members={"s1": {"a1", "a2", "a3"}})
    assert plan.status == "restack" and plan.stale_stacks == ["s1"]
    client, conn = _Client(), _Conn()
    result = sm.apply_plans(client, conn, [plan], log=lambda m: None)
    assert [c[0] for c in client.calls if c[0] in ("delete", "create")] == ["delete"]
    assert result.restacked == 1 and plan.applied and plan.applied_stack_id is None
    assert sm.receipts_after([plan], receipt, now=_T)["2026-09-09_19-16-35"].stack_id is None


def test_fetch_stack_members_sql_and_shape():
    conn = _Conn([("s1", "a1"), ("s1", "a2"), ("s2", "b1")])
    got = sm.fetch_stack_members(conn, {"s1", "s2", "s3"})
    ((sql, params),) = conn.calls
    assert '"stackId" = ANY(%s)' in sql and '"deletedAt" IS NULL' in sql and params == (["s1", "s2", "s3"],)
    assert got == {"s1": {"a1", "a2"}, "s2": {"b1"}, "s3": set()}
    assert sm.fetch_stack_members(_Conn(), set()) == {}


def test_apply_counts_tags_the_api_did_not_return():
    class _ShortClient(_Client):
        def upsert_tags(self, names):
            return {n: f"tag-{n}" for n in names if n != "Source/Tesla"}
    m = sm.parse_manifest(_manifest())
    (plan,) = sm.plan_groups(m, _assets([_row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3")]))
    result = sm.apply_plans(_ShortClient(), _Conn(), [plan], log=lambda m: None)
    assert result.created == 1 and result.tags_missing == 1 and result.tags_linked == 3


def test_fetch_folder_assets_scoped_to_library_and_root():
    conn = _Conn([_row("mosaic.mp4", "a1")])
    sm.fetch_folder_assets(conn, FOLDER, library_id="lib-1", container_root="/mnt/external/originals/")
    ((sql, params),) = conn.calls
    assert '"libraryId" = %s' in sql
    assert params == (rf"/mnt/external/originals/{FOLDER}/%", "lib-1")


def test_plan_missing_skips_group_and_names_paths():
    m = sm.parse_manifest(_manifest())
    (plan,) = sm.plan_groups(m, _assets([_row("mosaic.mp4", "a1"), _row("front.mp4", "a2")]))
    assert plan.status == "missing" and plan.missing == [f"{EVENT}/back.mp4"]
    assert not plan.resolved and plan.members == [] and plan.links == 0
    assert sm.archive_ids([plan]) == []


def test_plan_ambiguous_path_counts_as_missing():
    m = sm.parse_manifest(_manifest())
    (plan,) = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1"), _row("mosaic.mp4", "a9"),
        _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ]))
    assert plan.status == "missing" and "ambiguous" in plan.note


def test_tag_links_cover_every_member_times_tag_and_skip_unknown_tags():
    m = sm.parse_manifest(_manifest())
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ]))
    links = sm.tag_links(plans, {"Source/Tesla": "t1"})
    assert links == [("a1", "t1", "Source/Tesla"), ("a2", "t1", "Source/Tesla"),
                     ("a3", "t1", "Source/Tesla")]
    links = sm.tag_links(plans, {"Source/Tesla": "t1", "Tesla/Reason/honk": "t2"})
    assert len(links) == 6 and ("a3", "t2", "Tesla/Reason/honk") in links
    assert sm.all_tags(m) == ["Source/Tesla", "Tesla/Reason/honk"]


# --- write ------------------------------------------------------------------

class _Client:
    def __init__(self, fail_create: set[str] = frozenset()):
        self.calls: list[tuple] = []
        self.fail_create = set(fail_create)

    def upsert_tags(self, names):
        self.calls.append(("upsert", tuple(names)))
        return {n: f"tag-{n}" for n in names}

    def create_stack(self, primary_asset_id, other_asset_ids):
        self.calls.append(("create", primary_asset_id, tuple(other_asset_ids)))
        if primary_asset_id in self.fail_create:
            raise ImmichError("POST /api/stacks → 400")
        return "new-stack"

    def delete_stack(self, stack_id):
        self.calls.append(("delete", stack_id))

    def update_assets(self, asset_ids, *, is_archived):
        self.calls.append(("archive", tuple(asset_ids), is_archived))

    def tag_assets(self, *a, **k):
        raise AssertionError("tag-assign API must never be used")


def test_apply_create_links_tags_and_archives():
    m = sm.parse_manifest(_manifest(archive=True))
    conn = _Conn()
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ]))
    client = _Client()
    logs: list[str] = []
    result = sm.apply_plans(client, conn, plans, log=logs.append)
    assert client.calls == [
        ("upsert", ("Source/Tesla", "Tesla/Reason/honk")),
        ("create", "a1", ("a2", "a3")),
        ("archive", ("a1", "a2", "a3"), True),
    ]
    assert sorted(conn.links) == sorted(
        (a, f"tag-{t}", t) for a in ("a1", "a2", "a3")
        for t in ("Source/Tesla", "Tesla/Reason/honk"))
    assert conn.commits == 1
    assert (result.created, result.restacked, result.tags_linked, result.archived,
            result.failed) == (1, 0, 6, 3, 0)
    assert logs == []


def test_apply_restack_deletes_every_stale_stack_then_creates():
    m = sm.parse_manifest(_manifest())
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1", "s1", "a1", 2), _row("front.mp4", "a2", "s1", "a1", 2),
        _row("back.mp4", "a3", "s2", "a3", 1),
    ]), restack=True)
    client = _Client()
    result = sm.apply_plans(client, _Conn(), plans, log=lambda m: None)
    assert client.calls[1:] == [
        ("delete", "s1"), ("delete", "s2"), ("create", "a1", ("a2", "a3")),
    ]
    assert result.restacked == 1 and result.created == 0


def test_apply_ok_and_differs_touch_no_stacks_but_still_link_tags_and_skip_missing():
    data = _manifest()
    data["groups"].append({"key": "k2", "primary": "e2/mosaic.mp4",
                           "children": ["e2/front.mp4"], "tags": ["Source/Tesla"]})
    data["groups"].append({"key": "k3", "primary": "e3/mosaic.mp4", "tags": ["Lost"]})
    m = sm.parse_manifest(data)
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1", "s1", "a1", 3), _row("front.mp4", "a2", "s1", "a1", 3),
        _row("back.mp4", "a3", "s1", "a1", 3),
        _row("mosaic.mp4", "b1", "s9", "b2", 2, event="e2"),
        _row("front.mp4", "b2", "s9", "b2", 2, event="e2"),
    ]))
    assert [p.status for p in plans] == ["ok", "differs", "missing"]
    client = _Client()
    conn = _Conn()
    result = sm.apply_plans(client, conn, plans, log=lambda m: None)
    # the missing group's tag is never even created
    assert client.calls == [("upsert", ("Source/Tesla", "Tesla/Reason/honk"))]
    assert {a for a, _, _ in conn.links} == {"a1", "a2", "a3", "b1", "b2"}
    assert result.tags_linked == 6 + 2 and result.created == 0 and result.failed == 0


def test_apply_isolates_a_failing_group():
    data = _manifest()
    data["groups"].append({"key": "k2", "primary": "e2/mosaic.mp4",
                           "children": ["e2/front.mp4"], "tags": [], "archive": True})
    m = sm.parse_manifest(data)
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
        _row("mosaic.mp4", "b1", event="e2"), _row("front.mp4", "b2", event="e2"),
    ]))
    client = _Client(fail_create={"a1"})
    logs: list[str] = []
    result = sm.apply_plans(client, _Conn(), plans, log=logs.append)
    assert result.failed == 1 and result.created == 1 and result.archived == 2
    assert logs == ["group 2026-09-09_19-16-35: POST /api/stacks → 400"]
    assert ("archive", ("b1", "b2"), True) in client.calls


def test_apply_rolls_back_on_sql_failure():
    m = sm.parse_manifest(_manifest())
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ]))
    conn = _Conn()
    conn.cursor = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    logs: list[str] = []
    result = sm.apply_plans(_Client(), conn, plans, log=logs.append)
    assert result.failed == 1 and conn.rollbacks == 1
    assert logs == ["group 2026-09-09_19-16-35: db down"]


# --- receipt (re-trim asset-id check) -----------------------------------------

_T = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
_ROWS = [_row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3")]


def test_receipt_round_trip_and_errors(tmp_path: Path):
    assert sm.load_receipt(tmp_path) == {}
    rec = {"k": sm.Receipt("a1", ("a2",), "s1", "2026-10-11T12:00:00+00:00"),
           "j": sm.Receipt("b1", (), None, "2026-10-11T12:00:00+00:00")}
    path = sm.write_receipt(tmp_path, rec)
    assert path == tmp_path / ".audit" / "stacks-applied.json"
    assert sm.load_receipt(tmp_path) == rec
    assert list(json.loads(path.read_text())["groups"]) == ["j", "k"]
    path.write_text('{"schema": 1, "groups": {"k": {"primary_asset_id": "a1"}}}')
    with pytest.raises(sm.ManifestError, match="malformed entry"):
        sm.load_receipt(tmp_path)
    path.write_text('{"schema": 2, "groups": {}}')
    with pytest.raises(sm.ManifestError, match="not a schema-1 receipt"):
        sm.load_receipt(tmp_path)


def test_receipts_after_records_applied_groups_and_keeps_the_rest():
    m = sm.parse_manifest(_manifest())
    plans = sm.plan_groups(m, _assets(_ROWS))
    assert sm.receipts_after(plans, {}, now=_T) == {}  # dry: nothing applied
    client = _Client()
    sm.apply_plans(client, _Conn(), plans, log=lambda m: None)
    rec = sm.receipts_after(plans, {"other": sm.Receipt("z", (), None, "t")}, now=_T)
    assert rec["other"].primary_asset_id == "z"
    assert rec["2026-09-09_19-16-35"] == sm.Receipt(
        "a1", ("a2", "a3"), "new-stack", "2026-10-11T12:00:00+00:00")
    # an `ok` group records its live stack id; a failed one keeps the old entry
    plans = sm.plan_groups(m, _assets([
        _row("mosaic.mp4", "a1", "s1", "a1", 3), _row("front.mp4", "a2", "s1", "a1", 3),
        _row("back.mp4", "a3", "s1", "a1", 3)]))
    sm.apply_plans(_Client(), _Conn(), plans, log=lambda m: None)
    assert sm.receipts_after(plans, {}, now=_T)["2026-09-09_19-16-35"].stack_id == "s1"
    plans = sm.plan_groups(m, _assets(_ROWS))
    sm.apply_plans(_Client(fail_create={"a1"}), _Conn(), plans, log=lambda m: None)
    assert sm.receipts_after(plans, rec, now=_T) == rec


def test_plan_id_changed_is_left_alone_without_restack():
    m = sm.parse_manifest(_manifest(archive=True))
    receipt = {"2026-09-09_19-16-35": sm.Receipt("OLD", ("a2", "a3"), "s1", "2026-10-01T00:00:00+00:00")}
    # the re-rendered mosaic came back as a new asset `a1`; its old stack is gone
    (plan,) = sm.plan_groups(m, _assets(_ROWS), receipt=receipt)
    assert plan.status == "id-changed" and not plan.resolved
    assert "OLD" in plan.note and "since 2026-10-01" in plan.note
    assert plan.links == 0 and sm.archive_ids([plan]) == []
    client, conn = _Client(), _Conn()
    result = sm.apply_plans(client, conn, [plan], log=lambda m: None)
    assert client.calls == [] and conn.links == [] and result.created == 0
    assert sm.receipts_after([plan], receipt, now=_T) == receipt
    # the unchanged id passes straight through to the normal statuses
    (plan,) = sm.plan_groups(m, _assets(_ROWS),
                             receipt={"2026-09-09_19-16-35": sm.Receipt("a1", (), None, "t")})
    assert plan.status == "create"


def test_plan_id_changed_with_restack_rebuilds_and_updates_receipt():
    m = sm.parse_manifest(_manifest())
    receipt = {"2026-09-09_19-16-35": sm.Receipt("OLD", ("a2", "a3"), "s1", "2026-10-01T00:00:00+00:00")}
    rows = [_row("mosaic.mp4", "a1"), _row("front.mp4", "a2", "s1", "OLD", 3),
            _row("back.mp4", "a3", "s1", "OLD", 3)]
    # s1 holds the superseded OLD primary plus our two children: ours to dissolve
    (plan,) = sm.plan_groups(m, _assets(rows), receipt=receipt, restack=True,
                             stack_members={"s1": {"OLD", "a2", "a3"}})
    assert plan.status == "restack" and plan.stale_stacks == ["s1"]
    client = _Client()
    sm.apply_plans(client, _Conn(), [plan], log=lambda m: None)
    assert client.calls[1:] == [("delete", "s1"), ("create", "a1", ("a2", "a3"))]
    assert sm.receipts_after([plan], receipt, now=_T)["2026-09-09_19-16-35"] == sm.Receipt(
        "a1", ("a2", "a3"), "new-stack", "2026-10-11T12:00:00+00:00")


# --- ImmichClient additions --------------------------------------------------

def test_client_delete_stack_and_update_assets_shapes(monkeypatch):
    from immy.immich import ImmichClient

    client = ImmichClient(url="http://imm", api_key="k")
    calls: list[tuple] = []
    monkeypatch.setattr(client, "_request", lambda m, p, body=None: calls.append((m, p, body)))
    client.delete_stack("s1")
    client.update_assets(["a1", "a2"], is_archived=True)
    client.update_assets([], is_archived=True)
    client.update_assets(["a3"], is_archived=False)
    assert calls == [
        ("DELETE", "/api/stacks/s1", None),
        ("PUT", "/api/assets", {"ids": ["a1", "a2"], "visibility": "archive"}),
        ("PUT", "/api/assets", {"ids": ["a3"], "visibility": "timeline"}),
    ]


# --- CLI --------------------------------------------------------------------

def test_cli_help():
    result = runner.invoke(cli.app, ["stacks", "apply", "--help"])
    assert result.exit_code == 0, result.output
    assert "--restack" in result.output and "--write" in result.output


def _render_folder(tmp_path: Path, data=None) -> Path:
    folder = tmp_path / FOLDER
    folder.mkdir()
    (folder / "stacks.json").write_text(json.dumps(data or _manifest()))
    return folder


def _cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        f"state_root: {tmp_path / 'state'}\n"
        "immich: {url: http://imm, api_key: k, library_id: L}\n"
        "pg: {host: db, user: u, password: p, database: immich}\n"
    )
    return cfg


def test_cli_dry_run_reports_and_writes_nothing(monkeypatch, tmp_path: Path):
    folder = _render_folder(tmp_path)
    conn = _Conn([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ])
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: conn, raising=False)
    monkeypatch.setattr(cli.pg_mod, "fetch_library_info", lambda c, lib: types.SimpleNamespace(id="lib-1", container_root="/mnt/external/originals"), raising=False)
    client = _Client()
    monkeypatch.setattr(cli, "ImmichClient", lambda **kw: client)
    result = runner.invoke(cli.app, ["stacks", "apply", str(folder), "--config", str(_cfg(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "create" in result.output and "dry-run" in result.output
    assert "1 create" in result.output and "6 tag links" in result.output
    assert client.calls == [] and conn.links == [] and conn.closed


def test_cli_write_applies(monkeypatch, tmp_path: Path, no_schema_guard):
    folder = _render_folder(tmp_path)
    conn = _Conn([
        _row("mosaic.mp4", "a1"), _row("front.mp4", "a2"), _row("back.mp4", "a3"),
    ])
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: conn, raising=False)
    monkeypatch.setattr(cli.pg_mod, "fetch_library_info", lambda c, lib: types.SimpleNamespace(id="lib-1", container_root="/mnt/external/originals"), raising=False)
    client = _Client()
    monkeypatch.setattr(cli, "ImmichClient", lambda **kw: client)
    result = runner.invoke(cli.app, [
        "stacks", "apply", str(folder), "--write", "--config", str(_cfg(tmp_path)),
    ])
    assert result.exit_code == 0, result.output
    assert ("create", "a1", ("a2", "a3")) in client.calls
    assert len(conn.links) == 6 and "1 stacks created" in result.output
    receipt = json.loads((folder / ".audit" / "stacks-applied.json").read_text())
    entry = receipt["groups"]["2026-09-09_19-16-35"]
    assert (entry["primary_asset_id"], entry["child_asset_ids"], entry["stack_id"]) == (
        "a1", ["a2", "a3"], "new-stack")
    assert entry["applied_at"]

    # second run: the mosaic was re-rendered and came back as a NEW asset
    conn2 = _Conn([_row("mosaic.mp4", "a1-new"), _row("front.mp4", "a2"), _row("back.mp4", "a3")])
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: conn2, raising=False)
    client2 = _Client()
    monkeypatch.setattr(cli, "ImmichClient", lambda **kw: client2)
    result = runner.invoke(cli.app, [
        "stacks", "apply", str(folder), "--write", "--config", str(_cfg(tmp_path)),
    ])
    assert result.exit_code == 0, result.output
    assert "id-changed" in result.output and "1 id-changed" in result.output
    assert client2.calls == [] and conn2.links == []
    receipt = json.loads((folder / ".audit" / "stacks-applied.json").read_text())
    assert receipt["groups"]["2026-09-09_19-16-35"]["primary_asset_id"] == "a1"  # untouched

    # --restack rebuilds and re-records
    result = runner.invoke(cli.app, [
        "stacks", "apply", str(folder), "--write", "--restack", "--config", str(_cfg(tmp_path)),
    ])
    assert result.exit_code == 0, result.output
    assert ("create", "a1-new", ("a2", "a3")) in client2.calls and "1 restacked" in result.output
    receipt = json.loads((folder / ".audit" / "stacks-applied.json").read_text())
    assert receipt["groups"]["2026-09-09_19-16-35"]["primary_asset_id"] == "a1-new"


def test_cli_bad_manifest_exits_2(tmp_path: Path):
    folder = _render_folder(tmp_path, {"schema": 1, "groups": [], "bogus": 1})
    result = runner.invoke(cli.app, ["stacks", "apply", str(folder)])
    assert result.exit_code == 2 and "unknown manifest keys" in result.output
