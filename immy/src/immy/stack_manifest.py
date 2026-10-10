"""Apply a render folder's `stacks.json` to Immich: stacks, tags, archive.

A renderer (teslacam first, anything later) ships a folder through
`immy promote` and leaves a manifest beside the media that says which files
belong together as ONE Immich stack, which tags every member gets, and
whether the group is archived. `immy stacks apply <folder>` makes Immich
match the manifest and is safe to re-run: groups already stacked as asked
are `ok`, tag links are `ON CONFLICT DO NOTHING`, archiving an archived
asset is a no-op.

    {"schema": 1, "album": "Tesla Dashcam",
     "groups": [{"key": "2026-09-09_19-16-35",
                 "primary": "<event>/mosaic.mp4",
                 "children": ["<event>/front.mp4"],
                 "tags": ["Source/Tesla", "Tesla/Reason/honk"],
                 "archive": false}]}

Paths are relative to the folder; an asset is the one whose `originalPath`
ends with `/<folder name>/<relative path>`. `album` is informational —
`promote` owns albums.

Read path is Postgres (one query over the folder's assets, like
`stacks.fetch_candidates`). Write path:
- stacks: ONLY the Immich API (`POST`/`DELETE /api/stacks`), never SQL;
- tags: created through the API (`PUT /api/tags`), LINKED by SQL through
  `trips.link_tags` with `asset_exif.tags` locked — the tag-assign API
  wipes tags on read-only originals (see the note above `LOCK_TAG_SQL`);
- archive: `PUT /api/assets` (visibility). Never un-archives.

A `--write` run leaves a receipt, `<folder>/.audit/stacks-applied.json`
(group key → primary/child asset ids, stack id, time). Every later run,
dry or not, compares each group's resolved primary asset id with the
receipt: a mismatch means a re-rendered file at the same path became a NEW
Immich asset instead of being re-read in place — the pilot's re-trim
check. Such a group is `id-changed` and left alone; `--restack` rebuilds it
and updates the receipt. `.audit/` is already outside promote's rsync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .pg import like_prefix

SCHEMA = 1

_MANIFEST_KEYS = {"schema", "album", "groups"}
_GROUP_KEYS = {"key", "primary", "children", "tags", "archive"}

STATUS_CREATE = "create"
STATUS_OK = "ok"
STATUS_DIFFERS = "differs"
STATUS_RESTACK = "restack"
STATUS_MISSING = "missing"
STATUS_ID_CHANGED = "id-changed"

RECEIPT_REL = Path(".audit") / "stacks-applied.json"
RECEIPT_SCHEMA = 1


class ManifestError(ValueError):
    """`stacks.json` is malformed: wrong schema, unknown keys, bad paths."""


@dataclass(frozen=True)
class Group:
    key: str
    primary: str
    children: tuple[str, ...]
    tags: tuple[str, ...]
    archive: bool

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.primary, *self.children)


@dataclass(frozen=True)
class Manifest:
    schema: int
    album: str | None
    groups: tuple[Group, ...]


def _check_rel_path(where: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: path must be a non-empty string, got {value!r}")
    if value.startswith("/") or "\\" in value:
        raise ManifestError(f"{where}: {value!r} is not a relative POSIX path")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ManifestError(f"{where}: {value!r} must not contain '', '.' or '..' segments")
    return value


def _check_names(where: str, value: object, *, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ManifestError(f"{where}: {what} must be a list of non-empty strings")
    return tuple(dict.fromkeys(value))


def parse_manifest(data: object) -> Manifest:
    """Validate the decoded JSON strictly: unknown keys are errors (a typo
    like `childs` would otherwise silently un-stack a whole run)."""
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    unknown = set(data) - _MANIFEST_KEYS
    if unknown:
        raise ManifestError(f"unknown manifest keys: {sorted(unknown)}")
    if data.get("schema") != SCHEMA:
        raise ManifestError(f"unsupported schema {data.get('schema')!r} (want {SCHEMA})")
    album = data.get("album")
    if album is not None and not isinstance(album, str):
        raise ManifestError("album must be a string")
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list):
        raise ManifestError("groups must be a list")

    groups: list[Group] = []
    seen_keys: set[str] = set()
    seen_paths: dict[str, str] = {}
    for i, g in enumerate(raw_groups):
        where = f"groups[{i}]"
        if not isinstance(g, dict):
            raise ManifestError(f"{where}: must be an object")
        unknown = set(g) - _GROUP_KEYS
        if unknown:
            raise ManifestError(f"{where}: unknown keys {sorted(unknown)}")
        key = g.get("key")
        if not isinstance(key, str) or not key:
            raise ManifestError(f"{where}: key must be a non-empty string")
        if key in seen_keys:
            raise ManifestError(f"{where}: duplicate key {key!r}")
        seen_keys.add(key)
        where = f"group {key!r}"
        primary = _check_rel_path(where, g.get("primary"))
        raw_children = g.get("children", [])
        if not isinstance(raw_children, list):
            raise ManifestError(f"{where}: children must be a list")
        children = tuple(_check_rel_path(where, c) for c in raw_children)
        for p in (primary, *children):
            if p in seen_paths:
                owner = seen_paths[p]
                if owner == key:
                    raise ManifestError(f"{where}: {p!r} listed twice")
                raise ManifestError(f"{where}: {p!r} already belongs to group {owner!r}")
            seen_paths[p] = key
        tags = _check_names(where, g.get("tags", []), what="tags")
        for t in tags:
            if t.startswith("/") or t.endswith("/") or "//" in t:
                raise ManifestError(f"{where}: bad tag path {t!r}")
        archive = g.get("archive", False)
        if not isinstance(archive, bool):
            raise ManifestError(f"{where}: archive must be true/false")
        groups.append(Group(key=key, primary=primary, children=children,
                            tags=tags, archive=archive))
    return Manifest(schema=SCHEMA, album=album, groups=tuple(groups))


def load_manifest(path: Path) -> Manifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"no manifest at {path}") from None
    except json.JSONDecodeError as e:
        raise ManifestError(f"{path}: invalid JSON: {e}") from None
    return parse_manifest(data)


# --- receipt ----------------------------------------------------------------

@dataclass(frozen=True)
class Receipt:
    primary_asset_id: str
    child_asset_ids: tuple[str, ...]
    stack_id: str | None
    applied_at: str


def receipt_path(folder: Path) -> Path:
    return folder / RECEIPT_REL


def load_receipt(folder: Path) -> dict[str, Receipt]:
    """Group key → what the last `--write` recorded; `{}` when none yet."""
    path = receipt_path(folder)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ManifestError(f"{path}: invalid JSON: {e}") from None
    if (not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA
            or not isinstance(data.get("groups"), dict)):
        raise ManifestError(f"{path}: not a schema-{RECEIPT_SCHEMA} receipt")
    out: dict[str, Receipt] = {}
    for key, g in data["groups"].items():
        try:
            out[key] = Receipt(
                primary_asset_id=str(g["primary_asset_id"]),
                child_asset_ids=tuple(str(c) for c in g["child_asset_ids"]),
                stack_id=str(g["stack_id"]) if g.get("stack_id") else None,
                applied_at=str(g["applied_at"]),
            )
        except (KeyError, TypeError):
            raise ManifestError(f"{path}: malformed entry for group {key!r}") from None
    return out


def write_receipt(folder: Path, receipts: dict[str, Receipt]) -> Path:
    path = receipt_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema": RECEIPT_SCHEMA, "groups": {
        key: {"primary_asset_id": r.primary_asset_id,
              "child_asset_ids": list(r.child_asset_ids),
              "stack_id": r.stack_id, "applied_at": r.applied_at}
        for key, r in sorted(receipts.items())
    }}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def receipts_after(
    plans: list[GroupPlan], previous: dict[str, Receipt], *, now: datetime | None = None,
) -> dict[str, Receipt]:
    """The receipt to write after `apply_plans`: every group whose stack
    step succeeded this run (create / restack / ok) is recorded fresh; a
    group that was skipped, differs, or failed keeps its previous entry, so
    an `id-changed` keeps being reported until it is rebuilt. Before
    `apply_plans` (a dry run) nothing is applied and `previous` comes back
    unchanged."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    out = dict(previous)
    for plan in plans:
        if not plan.applied:
            continue
        assert plan.primary is not None
        out[plan.group.key] = Receipt(
            primary_asset_id=plan.primary.id,
            child_asset_ids=tuple(c.id for c in plan.children),
            stack_id=plan.applied_stack_id,
            applied_at=stamp,
        )
    return out


# --- read: Postgres ---------------------------------------------------------

@dataclass(frozen=True)
class Asset:
    id: str
    path: str                        # originalPath as Immich sees it
    stack_id: str | None
    stack_primary_id: str | None     # stack.primaryAssetId, when stacked
    stack_size: int                  # live members of that stack (0 if none)


# `~*`/LIKE on the folder marker so one round-trip covers every file the
# manifest can name; the correlated count gives each stack's live size
# without a second query (a folder is a few hundred rows at most). An
# asset whose stack reaches outside the folder shows up as `stack_size`
# larger than the group — that is a `differs`, never an `ok`.
FOLDER_ASSETS_SQL = """
SELECT a.id, a."originalPath", a."stackId", s."primaryAssetId",
       (SELECT count(*) FROM asset m
         WHERE m."stackId" = a."stackId" AND m."deletedAt" IS NULL)
FROM asset a
LEFT JOIN stack s ON s.id = a."stackId"
WHERE a."deletedAt" IS NULL AND a."originalPath" LIKE %s ESCAPE '\\'
"""


def folder_pattern(folder_name: str) -> str:
    """LIKE pattern for `…/<folder_name>/…` (wildcards in the name escaped)."""
    return "%/" + like_prefix(folder_name)[:-1] + "/%"


def fetch_folder_assets(conn, folder_name: str) -> dict[str, list[Asset]]:
    """Relative path (after `/<folder_name>/`) → assets ending in it.

    More than one asset for a relative path means the folder name recurs
    in the tree (`/x/2026-tesla/2026-tesla/a.mp4` yields both `a.mp4` and
    `2026-tesla/a.mp4`); the planner treats a manifest path that resolves
    to several assets as unresolvable rather than guessing."""
    marker = f"/{folder_name}/"
    out: dict[str, list[Asset]] = {}
    rows = conn.execute(FOLDER_ASSETS_SQL, (folder_pattern(folder_name),)).fetchall()
    for asset_id, path, stack_id, primary_id, size in rows:
        asset = Asset(
            id=str(asset_id), path=str(path),
            stack_id=str(stack_id) if stack_id else None,
            stack_primary_id=str(primary_id) if primary_id else None,
            stack_size=int(size or 0),
        )
        start = 0
        while (idx := path.find(marker, start)) != -1:
            out.setdefault(path[idx + len(marker):], []).append(asset)
            start = idx + 1
    return out


# --- plan -------------------------------------------------------------------

@dataclass
class GroupPlan:
    group: Group
    status: str
    primary: Asset | None = None
    children: list[Asset] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)     # unresolved rel paths
    stale_stacks: list[str] = field(default_factory=list)  # deleted on restack
    note: str = ""
    # set by apply_plans: stack step done (and the stack the group ended
    # up in), or the group failed
    applied: bool = False
    applied_stack_id: str | None = None
    failed: bool = False

    @property
    def members(self) -> list[Asset]:
        return [self.primary, *self.children] if self.primary else []

    @property
    def resolved(self) -> bool:
        """Acted on at all (tags, archive, stack). `missing` and
        `id-changed` groups are left untouched."""
        return self.status not in (STATUS_MISSING, STATUS_ID_CHANGED)

    @property
    def links(self) -> int:
        """(asset × tag) links this group asks for (0 when left alone)."""
        return len(self.members) * len(self.group.tags) if self.resolved else 0


def _resolve(rel: str, assets: dict[str, list[Asset]]) -> Asset | None:
    found = assets.get(rel, [])
    return found[0] if len(found) == 1 else None


def plan_groups(
    manifest: Manifest, assets: dict[str, list[Asset]], *, restack: bool = False,
    receipt: dict[str, Receipt] | None = None,
) -> list[GroupPlan]:
    """One plan per group, in manifest order.

    - any path unresolved                                   → missing (skipped)
    - receipt knows the group under another primary asset
      id (a re-render became a new asset)                   → id-changed
      (skipped), or restack when asked
    - no member stacked                                     → create
    - all members in ONE stack, primary as asked, no extras → ok
    - anything else (partial, other primary, foreign stack,
      extra members)                                        → differs,
      or restack when asked: every stack the members sit in
      is deleted through the API, then the group is created
    """
    receipt = receipt or {}
    plans: list[GroupPlan] = []
    for group in manifest.groups:
        primary = _resolve(group.primary, assets)
        children = [_resolve(c, assets) for c in group.children]
        missing = [p for p, a in zip(group.paths, (primary, *children)) if a is None]
        if missing:
            ambiguous = [p for p in missing if len(assets.get(p, [])) > 1]
            plans.append(GroupPlan(
                group=group, status=STATUS_MISSING, missing=missing,
                note=f"ambiguous: {', '.join(ambiguous)}" if ambiguous else "",
            ))
            continue
        assert primary is not None
        members: list[Asset] = [primary, *children]  # type: ignore[list-item]
        stack_ids = sorted({m.stack_id for m in members if m.stack_id})
        plan = GroupPlan(group=group, status=STATUS_CREATE, primary=primary,
                         children=list(children))  # type: ignore[arg-type]
        known = receipt.get(group.key)
        if known is not None and known.primary_asset_id != primary.id:
            plan.status = STATUS_RESTACK if restack else STATUS_ID_CHANGED
            plan.stale_stacks = stack_ids
            plan.note = (f"primary {known.primary_asset_id[:8]}… → {primary.id[:8]}… "
                         f"since {known.applied_at[:10]}")
        elif not stack_ids:
            pass
        elif (len(stack_ids) == 1
              and all(m.stack_id == stack_ids[0] for m in members)
              and primary.stack_primary_id == primary.id
              and primary.stack_size == len(members)):
            plan.status = STATUS_OK
        else:
            plan.status = STATUS_RESTACK if restack else STATUS_DIFFERS
            plan.stale_stacks = stack_ids
            plan.note = _differs_note(members, stack_ids, primary)
        plans.append(plan)
    return plans


def _differs_note(members: list[Asset], stack_ids: list[str], primary: Asset) -> str:
    if len(stack_ids) > 1:
        return f"members span {len(stack_ids)} stacks"
    if any(m.stack_id is None for m in members):
        return "some members unstacked"
    if primary.stack_primary_id != primary.id:
        return "other primary"
    return f"stack has {primary.stack_size} members, manifest {len(members)}"


def tag_links(plans: list[GroupPlan], tag_ids: dict[str, str]) -> list[tuple[str, str, str]]:
    """`(asset id, tag id, tag value)` for every member × group tag, for
    `trips.link_tags`. A tag `upsert_tags` did not return (API hiccup) is
    skipped — the re-run links it."""
    links: list[tuple[str, str, str]] = []
    for plan in plans:
        if not plan.resolved:
            continue
        for member in plan.members:
            for value in plan.group.tags:
                tag_id = tag_ids.get(value)
                if tag_id:
                    links.append((member.id, tag_id, value))
    return list(dict.fromkeys(links))


def archive_ids(plans: list[GroupPlan]) -> list[str]:
    """Members of resolved groups flagged `archive: true` (never the reverse)."""
    return [m.id for plan in plans if plan.resolved and plan.group.archive
            for m in plan.members]


def all_tags(manifest: Manifest) -> list[str]:
    return list(dict.fromkeys(t for g in manifest.groups for t in g.tags))


# --- write: API for stacks/archive, SQL for tag links ----------------------

@dataclass
class ApplyResult:
    created: int = 0
    restacked: int = 0
    failed: int = 0
    tags_linked: int = 0
    archived: int = 0


def apply_plans(client, conn, plans: list[GroupPlan], *, log) -> ApplyResult:
    """Per group: delete stale stacks (restack) → create → link tags → archive.
    A failing group is logged and counted, never fatal, like
    `stacks.apply_stacks` — the re-run picks up where it stopped (`ok`
    groups skip the stack step, links and archive are idempotent)."""
    from .immich import ImmichError
    from .trips import link_tags

    result = ApplyResult()
    wanted = all_tags(Manifest(schema=SCHEMA, album=None,
                               groups=tuple(p.group for p in plans if p.resolved)))
    tag_ids: dict[str, str] = {}
    if wanted:
        try:
            tag_ids = client.upsert_tags(wanted)
        except ImmichError as e:
            log(f"tags: upsert failed, no links this run: {e}")
        for name in wanted:
            if name not in tag_ids:
                log(f"tags: {name!r} not returned by upsert, skipped")

    for plan in plans:
        if not plan.resolved:
            continue
        label = plan.group.key
        try:
            if plan.status == STATUS_RESTACK:
                for stack_id in plan.stale_stacks:
                    client.delete_stack(stack_id)
            if plan.status in (STATUS_CREATE, STATUS_RESTACK):
                assert plan.primary is not None
                plan.applied_stack_id = client.create_stack(
                    primary_asset_id=plan.primary.id,
                    other_asset_ids=[c.id for c in plan.children],
                )
                if plan.status == STATUS_RESTACK:
                    result.restacked += 1
                else:
                    result.created += 1
            elif plan.status == STATUS_OK and plan.primary is not None:
                plan.applied_stack_id = plan.primary.stack_id
            if plan.status != STATUS_DIFFERS:
                plan.applied = True  # the stack is as the manifest says
            links = tag_links([plan], tag_ids)
            if links:
                link_tags(conn, links)
                result.tags_linked += len(links)
            if plan.group.archive:
                client.update_assets([m.id for m in plan.members], is_archived=True)
                result.archived += len(plan.members)
        except ImmichError as e:
            result.failed += 1
            plan.failed = True
            log(f"group {label}: {e}")
        except Exception as e:  # SQL side: roll the group back, keep going
            result.failed += 1
            plan.failed = True
            try:
                conn.rollback()
            except Exception:
                pass
            log(f"group {label}: {e}")
    return result
