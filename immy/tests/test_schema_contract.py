"""Every Postgres write in `src/immy` against the pinned Immich schema.

Extracts INSERT / UPDATE / DELETE statements from the string literals in
the source (AST, so implicit concatenation and module-level f-string
fragments resolve) and checks them against `data/immich_schema.json` and
`schema_contract.WRITE_COLUMNS`. Local SQLite writes (dedup manifest,
triage, snapshot DB) are recognised and set aside — see `_is_local_sqlite`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from immy import schema_contract

SRC = Path(schema_contract.__file__).parent

# Packages that only ever talk to immy's own SQLite files.
_SQLITE_PACKAGES = ("dedup", "triage")
# snapshot.py writes both Immich PG (no) and its own SQLite export (yes);
# these are its SQLite tables, written with an f-string placeholder list.
_LOCAL_SQLITE_TABLES = {("snapshot.py", "assets"), ("snapshot.py", "albums"),
                        ("snapshot.py", "album_assets")}

_IDENT = r'"?([A-Za-z_][A-Za-z0-9_]*)"?'
_STATEMENT = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+" + _IDENT,
    re.IGNORECASE,
)


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "..."` constants, for resolving f-string parts."""
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _joined_text(node: ast.JoinedStr, names: dict[str, str]) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v.value, ast.Name) and v.value.id in names:
            parts.append(names[v.value.id])
        else:
            parts.append("{?}")
    return "".join(parts)


def _piece_text(node: ast.AST, names: dict[str, str]) -> str | None:
    """Text of a string piece used in SQL assembly, or None if unresolvable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.JoinedStr):
        return _joined_text(node, names)
    return None


def _assembled_sql(tree: ast.Module, names: dict[str, str], rel: str):
    """SQL built incrementally inside a function — `sql = "UPDATE ..."` then
    `sql += FRAGMENT` (srtgeo's GPS lock). Every append is folded in, as if
    each conditional branch were taken, so all columns the statement can
    write are seen. Yields (text, lineno, ids of the pieces consumed).
    An append the extractor can't resolve fails loudly instead of letting
    a column slip past the contract."""
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        buffers: dict[str, list] = {}  # name -> [text, lineno, piece ids, appended?]
        nodes = sorted(
            (n for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AugAssign))),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for n in nodes:
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                text = _piece_text(n.value, names)
                if text is not None and _STATEMENT.search(text):
                    buffers[n.targets[0].id] = [text, n.lineno, {id(n.value)}, False]
            elif (isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add)
                    and isinstance(n.target, ast.Name) and n.target.id in buffers):
                buf = buffers[n.target.id]
                text = _piece_text(n.value, names)
                if text is None:
                    raise AssertionError(
                        f"{rel}:{n.lineno}: unsupported SQL assembly "
                        f"`{n.target.id} += {ast.unparse(n.value)}` — use a "
                        "string literal or a module-level constant"
                    )
                buf[0] += text
                buf[2].add(id(n.value))
                buf[3] = True
        for text, lineno, ids, appended in buffers.values():
            if appended:
                yield text, lineno, ids


def _split_top_level(text: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _paren_body(text: str, start: int) -> str:
    """Body of the parenthesised group opening at text[start] == '('."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise AssertionError(f"unbalanced parens in: {text!r}")


def _set_columns(clause: str) -> list[str]:
    cols = []
    for item in _split_top_level(clause):
        m = re.match(r"\s*" + _IDENT + r"\s*=", item)
        if m:
            cols.append(m.group(1))
    return cols


_CLAUSE_END = re.compile(r"\b(WHERE|RETURNING|FROM)\b|;|$", re.IGNORECASE)


def parse_statement(sql: str, start: int) -> dict | None:
    """Parse the write statement starting at `sql[start]`; None when the
    match isn't one (`DO UPDATE` inside an upsert, prose)."""
    m = _STATEMENT.match(sql, start)
    verb = m.group(0).split()[0].upper()
    table = m.group(1)
    rest = sql[m.end():]
    stmt = {"verb": verb, "table": table, "columns": [], "conflict": []}
    if verb == "INSERT":
        lead = rest.lstrip()
        if not lead.startswith("("):
            stmt["columns"] = None  # positional VALUES — no column list
        else:
            body = _paren_body(rest, rest.index("("))
            stmt["columns"] = [c.strip().strip('"') for c in body.split(",")]
        oc = re.search(r"ON\s+CONFLICT\s*\(", rest, re.IGNORECASE)
        if oc:
            body = _paren_body(rest, oc.end() - 1)
            stmt["conflict"] = [c.strip().strip('"') for c in body.split(",")]
        du = re.search(r"DO\s+UPDATE\s+SET\b", rest, re.IGNORECASE)
        if du:
            tail = rest[du.end():]
            end = _CLAUSE_END.search(tail)
            stmt["conflict"] += _set_columns(tail[:end.start()])
    elif verb == "UPDATE":
        sm = re.match(r"\s+SET\b", rest, re.IGNORECASE)
        if sm is None:
            return None
        tail = rest[sm.end():]
        end = _CLAUSE_END.search(tail)
        stmt["columns"] = _set_columns(tail[:end.start()])
    return stmt


_QMARK_PARAM = re.compile(r"[=(,]\s*\?")  # sqlite3 paramstyle; psycopg never uses it


def _is_local_sqlite(rel: str, text: str, table: str) -> bool:
    return (rel.split("/")[0] in _SQLITE_PACKAGES or bool(_QMARK_PARAM.search(text))
            or (rel, table) in _LOCAL_SQLITE_TABLES)


def extract_pg_writes(src: Path = SRC) -> list[dict]:
    out = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        tree = ast.parse(path.read_text())
        # Skip docstrings: prose like "INSERT INTO asset_exif ... DO NOTHING".
        docstrings = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        # f-string pieces are scanned as part of their JoinedStr, not alone.
        skip = docstrings | {
            id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values
        }
        names = _module_strings(tree)
        texts: list[tuple[str, int]] = []
        if rel.split("/")[0] not in _SQLITE_PACKAGES:
            for text, lineno, ids in _assembled_sql(tree, names, rel):
                texts.append((text, lineno))
                skip |= ids  # the pieces are scanned as the whole statement
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                texts.append((node.value, node.lineno))
            elif isinstance(node, ast.JoinedStr):
                texts.append((_joined_text(node, names), node.lineno))
        for text, lineno in texts:
            for m in _STATEMENT.finditer(text):
                stmt = parse_statement(text, m.start())
                if stmt is None or _is_local_sqlite(rel, text, stmt["table"]):
                    continue
                stmt["file"] = f"{rel}:{lineno}"
                out.append(stmt)
    return out


WRITES = extract_pg_writes()
SNAPSHOT = schema_contract.load_snapshot()["tables"]


def _ids(stmts):
    return [f"{s['file']} {s['verb']} {s['table']}" for s in stmts]


def test_extraction_finds_the_known_writes():
    # Guard against the extractor silently matching nothing (a regex
    # regression would otherwise make every test below vacuously pass).
    assert len(WRITES) >= 20, _ids(WRITES)
    inserts = {s["table"] for s in WRITES if s["verb"] == "INSERT"}
    assert inserts == set(schema_contract.INSERT_TABLES)
    files = {s["file"].split(":")[0] for s in WRITES}
    assert {"process.py", "offline.py", "pg.py", "promote.py",
            "backfill_dates.py", "srtgeo.py", "tagsync.py"} <= files


@pytest.mark.parametrize("stmt", WRITES, ids=_ids(WRITES))
def test_write_targets_exist_in_snapshot(stmt):
    assert stmt["table"] in SNAPSHOT, f"{stmt['file']}: unknown table {stmt['table']}"
    if stmt["verb"] == "INSERT":
        assert stmt["columns"] is not None, f"{stmt['file']}: INSERT without a column list"
    if stmt["verb"] == "UPDATE":
        assert stmt["columns"], f"{stmt['file']}: could not parse the SET clause"
    have = SNAPSHOT[stmt["table"]]
    unknown = [c for c in (stmt["columns"] or []) + stmt["conflict"] if c not in have]
    assert not unknown, (
        f"{stmt['file']}: {stmt['table']} has no column(s) {unknown} in Immich "
        f"{schema_contract.load_snapshot()['immich_version']}"
    )


@pytest.mark.parametrize(
    "stmt", [s for s in WRITES if s["verb"] == "INSERT"],
    ids=_ids([s for s in WRITES if s["verb"] == "INSERT"]),
)
def test_inserts_supply_every_required_column(stmt):
    required = schema_contract.required_columns(SNAPSHOT[stmt["table"]])
    missing = required - set(stmt["columns"] or [])
    assert not missing, f"{stmt['file']}: INSERT into {stmt['table']} omits NOT NULL {sorted(missing)}"


def test_write_columns_contract_matches_source():
    """`WRITE_COLUMNS` (what doctor and the runtime guard check) is exactly
    what the source writes — neither stale entries nor unguarded columns."""
    written: dict[str, set[str]] = {}
    for s in WRITES:
        written.setdefault(s["table"], set()).update(s["columns"] or [], s["conflict"])
    declared = {t: set(c) for t, c in schema_contract.WRITE_COLUMNS.items()}
    assert written == declared


def test_write_columns_exist_in_snapshot():
    for table, cols in schema_contract.WRITE_COLUMNS.items():
        assert set(cols) <= set(SNAPSHOT[table]), table


# --- extractor self-tests: it must catch what it is meant to catch -----------


def _parse(sql):
    return parse_statement(sql, _STATEMENT.search(sql).start())


def test_parser_reads_insert_columns_and_upsert():
    s = _parse('INSERT INTO asset_file ("assetId", type) VALUES (%s, %s) '
               'ON CONFLICT ("assetId", type) DO UPDATE SET path = EXCLUDED.path')
    assert s["columns"] == ["assetId", "type"]
    assert s["conflict"] == ["assetId", "type", "path"]


def test_parser_reads_update_set_with_nested_commas():
    s = _parse('UPDATE asset_exif SET description = %(d)s, "lockedProperties" = '
               "CASE WHEN 'x' = ANY(coalesce(a, '{}')) THEN a ELSE array_append(a, 'x') END "
               'WHERE "assetId" = %(id)s')
    assert s["columns"] == ["description", "lockedProperties"]


def test_extractor_folds_incremental_sql_assembly():
    # srtgeo builds its GPS UPDATE with `sql += _LOCK_GPS_FRAGMENT`.
    gps = [s for s in WRITES if s["file"].startswith("srtgeo.py")
           and "latitude" in (s["columns"] or [])]
    assert len(gps) == 1
    assert gps[0]["columns"] == ["latitude", "longitude", "lockedProperties"]


def test_extractor_fails_on_unresolvable_sql_assembly():
    tree = ast.parse(
        "def f(extra):\n"
        "    sql = 'UPDATE asset SET width = %(w)s'\n"
        "    sql += extra\n"
    )
    with pytest.raises(AssertionError, match="unsupported SQL assembly"):
        list(_assembled_sql(tree, {}, "x.py"))


def test_snapshot_flags_dropped_device_columns():
    # The 3.0.2 regression this contract exists for.
    assert "deviceAssetId" not in SNAPSHOT["asset"]
    assert "deviceId" not in SNAPSHOT["asset"]
    assert SNAPSHOT["asset"]["duration"]["udt_name"] == "int4"


# --- live guard -------------------------------------------------------------


class _FakeLivePg:
    """Answers information_schema.columns from a (mutable) copy of the snapshot."""

    def __init__(self, tables):
        self.tables = tables

    def execute(self, sql, params=()):
        assert "information_schema.columns" in sql
        cols = self.tables.get(params[0], {})
        rows = [
            (name, c["udt_name"], "YES" if c["is_nullable"] else "NO", c["column_default"])
            for name, c in cols.items()
        ]

        class _Cur:
            def fetchall(self_inner):
                return rows
        return _Cur()


def _live_copy():
    import copy
    return copy.deepcopy(SNAPSHOT)



def test_live_guard_passes_on_snapshot_schema():
    assert all(not p for p in schema_contract.live_schema_problems(_FakeLivePg(_live_copy())).values())
    schema_contract.assert_live_schema(_FakeLivePg(_live_copy()))


def test_live_guard_rejects_missing_retyped_and_newly_required_columns():
    live = _live_copy()
    del live["asset_file"]["isProgressive"]
    live["asset"]["duration"]["udt_name"] = "varchar"
    live["asset"]["visibility2"] = {"udt_name": "text", "is_nullable": False, "column_default": None}
    del live["smart_search"]
    problems = schema_contract.live_schema_problems(_FakeLivePg(live))
    assert problems["asset_file"] == ["missing columns: isProgressive"]
    assert problems["asset"] == [
        "changed type: duration is varchar (expected int4)",
        "new NOT NULL columns without default: visibility2",
    ]
    assert problems["smart_search"] == ["table missing"]
    assert problems["asset_exif"] == []
    with pytest.raises(schema_contract.SchemaMismatch, match="Refusing to write"):
        schema_contract.assert_live_schema(_FakeLivePg(live))


def test_live_guard_ignores_new_required_columns_on_update_only_tables():
    live = _live_copy()
    live["person"]["mood"] = {"udt_name": "text", "is_nullable": False, "column_default": None}
    assert schema_contract.live_schema_problems(_FakeLivePg(live))["person"] == []


def test_read_columns_exist_in_snapshot():
    missing = {t: [c for c in cols if c not in SNAPSHOT.get(t, {})]
               for t, cols in schema_contract.READ_COLUMNS.items()}
    assert {t: c for t, c in missing.items() if c} == {}


def test_live_guard_rejects_a_renamed_read_column():
    # 3.3 renamed asset_face.personId → personGroupId; the reverse must fail.
    live = _live_copy()
    live["person"]["personId"] = live["person"].pop("personGroupId")
    del live["stack"]
    problems = schema_contract.live_schema_problems(_FakeLivePg(live))
    assert problems["person"] == ["missing read columns: personGroupId"]
    assert problems["stack"] == ["table missing"]
