"""Read-only questions to the Immich database: "is this probably already in
the library?" (a hint for the reviewer) and "is this library path taken?"
(a stage check). External-library checksums here are path hashes, so file
content cannot be compared; time + duration (video) or time + size (still)
give candidates. `asset.duration` is milliseconds; `localDateTime` is the
local wall clock stored as UTC."""

from __future__ import annotations

from pathlib import Path

WALL = ('(a."localDateTime" at time zone \'UTC\')::date = %(t)s::date '
        'and abs(extract(epoch from (a."localDateTime" at time zone \'UTC\') - %(t)s::timestamp)) <= %(w)s')
INSTANT = ('a."fileCreatedAt" between %(t)s::timestamptz - make_interval(secs => %(w)s) '
           'and %(t)s::timestamptz + make_interval(secs => %(w)s)')


class Immich:
    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def connect(cls, config: Path) -> "Immich":
        import psycopg
        import yaml
        c = yaml.safe_load(Path(config).read_text())["pg"]
        return cls(psycopg.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"],
                                   dbname=c["database"], connect_timeout=5, autocommit=True))

    def _q(self, sql: str, args: dict) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return [r[0] for r in cur.fetchall()]

    def candidates(self, item: dict) -> list[str]:
        kind = item.get("time_kind")
        if kind not in ("wall", "instant") or not item.get("time"):
            return []
        video = item["kind"] == "video"
        when = WALL if kind == "wall" else INSTANT
        args = {"t": item["time"] if kind == "wall" else item["utc"], "w": 5 if video else 2}
        if video:
            args["d"] = int(round((item.get("duration") or 0) * 1000))
            sql = (f'select a."originalPath" from asset a where a."deletedAt" is null and a.type = \'VIDEO\' '
                   f'and {when} and abs(coalesce(a.duration, 0) - %(d)s) <= 1000 order by 1')
        else:
            args["s"] = item.get("primary_size")
            sql = (f'select a."originalPath" from asset a join asset_exif e on e."assetId" = a.id '
                   f'where a."deletedAt" is null and e."fileSizeInByte" = %(s)s and {when} order by 1')
        return self._q(sql, args)

    def existing_paths(self, paths: list[str]) -> set[str]:
        if not paths:
            return set()
        return set(self._q('select "originalPath" from asset where "originalPath" = any(%(p)s) '
                           'and "deletedAt" is null', {"p": list(paths)}))
