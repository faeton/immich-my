"""Backfill Immich stacks for Insta360 recordings.

One 360 recording lands in Immich as up to four timeline tiles: the two
`VID_*.insv` lens masters (`_00_` front / `_10_` back fisheye — unwatchable
alone), the camera's stitched low-res preview (`LRV_*_11_*.insv`), and
sometimes a stitched full-res `.mp4` export. `promote` stacks new pairs at
upload time (see `_stack_pair`), but the pre-existing library was promoted
before that existed and dual-lens trios were never covered — this module
sweeps the whole Immich asset table and folds every recording into ONE
stack.

Primary choice, best-watchable first: stitched mp4 export → LRV preview
(same order the timeline should surface them) → front lens master.
Grouping key is the (timestamp, serial) pair the camera stamps into every
file of a recording — deliberately NOT directory-scoped, so a stitched
export that lives in another folder still joins its masters' stack.

Read path is Postgres (one query over the whole asset table — the REST
search API would need a round-trip per name); the WRITE path is strictly
the Immich API (`POST /api/stacks`), never SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = re.compile(
    r"^(?P<role>VID|LRV)_(?P<ts>\d{8}_\d{6})_(?P<lens>\d{2})_(?P<serial>\d+)"
    r"\.(?P<ext>insv|mp4)$",
    re.I,
)

# SQL-side prefilter for the same shape (POSIX regex, case-insensitive).
PG_NAME_PATTERN = r"/(VID|LRV)_\d{8}_\d{6}_\d{2}_\d+\.(insv|mp4)$"


def _rank(role: str, lens: str, ext: str) -> int:
    """Lower = better primary. A stitched VID export beats the LRV preview
    beats the raw fisheye masters — role first, then ext: LRV_*.mp4 is
    still a low-res preview and must never outrank a VID_*.mp4 master."""
    if role == "vid" and ext == "mp4":
        return 0
    if role == "lrv":
        return 1
    return 2 if lens == "00" else 3


@dataclass
class StackPlan:
    key: tuple[str, str]                  # (timestamp, serial)
    primary: tuple[str, str]              # (asset_id, filename)
    children: list[tuple[str, str]]


def plan_stacks(
    rows: list[tuple[str, str, str | None]],
) -> tuple[list[StackPlan], int, int]:
    """rows: (asset_id, original_path, stack_id) for every candidate asset.

    Returns (plans, already_stacked_groups, singletons). A group where ANY
    member already sits in a stack is skipped whole — merging into existing
    stacks or re-parenting is a manual job, not a backfill's."""
    groups: dict[tuple[str, str], list[tuple[int, str, str, str | None]]] = {}
    for asset_id, path, stack_id in rows:
        name = path.rsplit("/", 1)[-1]
        m = _NAME.match(name)
        if not m:
            continue
        rank = _rank(m["role"].lower(), m["lens"], m["ext"].lower())
        key = (m["ts"], m["serial"])
        groups.setdefault(key, []).append((rank, asset_id, name, stack_id))

    plans: list[StackPlan] = []
    already = singles = 0
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            singles += 1
            continue
        if any(stack_id for _, _, _, stack_id in members):
            already += 1
            continue
        members.sort()
        plans.append(StackPlan(
            key=key,
            primary=(members[0][1], members[0][2]),
            children=[(aid, name) for _, aid, name, _ in members[1:]],
        ))
    return plans, already, singles


def fetch_candidates(pg_conn) -> list[tuple[str, str, str | None]]:
    return [
        (str(asset_id), path, str(stack_id) if stack_id else None)
        for asset_id, path, stack_id in pg_conn.execute(
            'SELECT id, "originalPath", "stackId" FROM asset'
            ' WHERE "deletedAt" IS NULL AND "originalPath" ~* %s',
            (PG_NAME_PATTERN,),
        ).fetchall()
    ]


def apply_stacks(client, plans: list[StackPlan], log) -> tuple[int, int]:
    """POST each plan; per-group errors are logged and counted, never fatal
    (a half-done backfill just re-runs — already-stacked groups skip)."""
    from .immich import ImmichError

    done = failed = 0
    for plan in plans:
        try:
            client.create_stack(
                primary_asset_id=plan.primary[0],
                other_asset_ids=[aid for aid, _ in plan.children],
            )
            done += 1
        except ImmichError as e:
            failed += 1
            log(f"stack {plan.primary[1]}: {e}")
    return done, failed
