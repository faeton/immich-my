"""Insta360 stack backfill: grouping, primary ranking, skip rules."""

from __future__ import annotations

from immy import stacks


def test_plan_stacks_groups_ranks_and_skips():
    rows = [
        # full trio + a stitched export living in ANOTHER directory
        ("a", "/lib/2024-02-x/VID_20240211_125134_00_053.insv", None),
        ("b", "/lib/2024-02-x/VID_20240211_125134_10_053.insv", None),
        ("c", "/lib/2024-02-x/LRV_20240211_125134_11_053.insv", None),
        ("d", "/lib/2024/02/VID_20240211_125134_00_053.mp4", None),
        # lone recording — nothing to stack
        ("e", "/lib/2024-02-x/VID_20240301_000000_00_001.insv", None),
        # pair where one member is already stacked → whole group skipped
        ("f", "/lib/t/VID_20240401_000000_00_002.insv", "s1"),
        ("g", "/lib/t/VID_20240401_000000_10_002.insv", None),
        # non-insta360 name → ignored entirely
        ("h", "/lib/t/IMG_1234.jpg", None),
    ]
    plans, already, singles = stacks.plan_stacks(rows)
    assert (already, singles) == (1, 1)
    assert len(plans) == 1
    plan = plans[0]
    # export beats LRV preview beats front lens beats back lens
    assert plan.primary == ("d", "VID_20240211_125134_00_053.mp4")
    assert [aid for aid, _ in plan.children] == ["c", "a", "b"]


def test_plan_stacks_new_gen_naming():
    """X4/X5 era: LRV preview has ext .lrv, app exports are 360VID_*.mp4."""
    rows = [
        ("a", "/lib/t/VID_20260226_070904_00_006.insv", None),
        ("b", "/lib/t/LRV_20260226_070904_01_006.lrv", None),
        ("c", "/lib/t/360VID_20260226_070904_00_006.mp4", None),
    ]
    plans, _, _ = stacks.plan_stacks(rows)
    assert len(plans) == 1
    assert plans[0].primary[0] == "c"           # stitched export wins
    assert [aid for aid, _ in plans[0].children] == ["b", "a"]


def test_plan_stacks_lrv_primary_without_export():
    rows = [
        ("a", "/lib/t/VID_20240211_125134_00_053.insv", None),
        ("b", "/lib/t/VID_20240211_125134_10_053.insv", None),
        ("c", "/lib/t/LRV_20240211_125134_11_053.insv", None),
    ]
    plans, _, _ = stacks.plan_stacks(rows)
    assert plans[0].primary[0] == "c"


def test_apply_stacks_survives_per_group_errors():
    from immy.immich import ImmichError

    calls = []

    class FakeClient:
        def create_stack(self, primary_asset_id, other_asset_ids):
            calls.append((primary_asset_id, other_asset_ids))
            if primary_asset_id == "bad":
                raise ImmichError("boom")

    plans = [
        stacks.StackPlan(("t", "1"), ("ok", "a.mp4"), [("x", "b.insv")]),
        stacks.StackPlan(("t", "2"), ("bad", "c.mp4"), [("y", "d.insv")]),
    ]
    logged = []
    done, failed = stacks.apply_stacks(FakeClient(), plans, log=logged.append)
    assert (done, failed) == (1, 1)
    assert len(calls) == 2 and "boom" in logged[0]
