#!/usr/bin/env python3
"""Regenerate `src/immy/data/immich_schema.json` from a live Immich DB.

Snapshots every table immy writes or reads (`schema_contract.WRITE_COLUMNS`,
`READ_COLUMNS`) from
information_schema — no pg_dump needed. Read-only: the connection is put in
read-only mode before the first query.

Run after an Immich upgrade, from `immy/`:

    uv run python scripts/regen_immich_schema.py --immich-version 3.0.2 \
        [--config /path/config.yml] [--host 127.0.0.1 --port 15432]

then `uv run pytest tests/test_schema_contract.py` — it fails on every
statement the new schema breaks.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from immy import config as config_mod
from immy import pg as pg_mod
from immy import schema_contract

_COLUMNS_SQL = """
SELECT column_name, data_type, udt_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = %s
ORDER BY ordinal_position
"""

_MIGRATION_SQL = "SELECT name FROM kysely_migrations ORDER BY name DESC LIMIT 1"


def snapshot(conn, immich_version: str) -> dict:
    tables: dict[str, dict] = {}
    for table in sorted(set(schema_contract.WRITE_COLUMNS) | set(schema_contract.READ_COLUMNS)):
        rows = conn.execute(_COLUMNS_SQL, (table,)).fetchall()
        if not rows:
            raise SystemExit(f"table {table} not found — wrong database?")
        tables[table] = {
            name: {
                "data_type": data_type,
                "udt_name": udt,
                "is_nullable": nullable == "YES",
                "column_default": default,
            }
            for name, data_type, udt, nullable, default in rows
        }
    row = conn.execute(_MIGRATION_SQL).fetchone()
    return {
        "immich_version": immich_version,
        "latest_migration": row[0] if row else None,
        "generated_by": "immy/scripts/regen_immich_schema.py",
        "tables": tables,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--immich-version", required=True)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--out", type=Path, default=schema_contract.SNAPSHOT_PATH)
    args = ap.parse_args()

    cfg = config_mod.load(args.config).pg
    if cfg is None:
        raise SystemExit("no pg: section in immy config")
    overrides = {k: v for k, v in (("host", args.host), ("port", args.port)) if v}
    cfg = dataclasses.replace(cfg, **overrides)

    conn = pg_mod.connect(cfg)
    conn.read_only = True
    try:
        data = snapshot(conn, args.immich_version)
    finally:
        conn.close()
    args.out.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {args.out} ({len(data['tables'])} tables, {data['latest_migration']})")


if __name__ == "__main__":
    main()
