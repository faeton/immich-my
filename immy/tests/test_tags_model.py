"""`immy tags model`: Gear/Camera/<model> for assets with no trip notes."""

from __future__ import annotations

from typer.testing import CliRunner

from immy import cli


class _Cur:
    def __init__(self, db):
        self.db, self._rows = db, []

    def execute(self, sql, params=None):
        if 'FROM "user"' in sql:
            self._rows = [("u1", "me@example.com")]
        elif "ILIKE ANY" in sql:
            self.db["patterns"] = params[1]
            self._rows = [("a1", "Ray-Ban Meta Smart Glasses 2"), ("a2", "Ray-Ban Meta Smart Glasses 2 "),
                          ("a3", "Meta Smart Glasses")]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cur(self.db)

    def close(self):
        pass


class _Api:
    def __init__(self, **kw):
        pass

    def upsert_tags(self, names):
        return {n: f"id:{n}" for n in names}


def test_tags_model_links_one_tag_per_model(tmp_path, monkeypatch) -> None:
    db = {}
    cfg = tmp_path / "config.yml"
    cfg.write_text("pg: {host: h, port: 1, user: u, password: p, database: d}\n"
                   "immich: {url: http://x, api_key: k, library_id: l}\n")
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: _Conn(db))
    monkeypatch.setattr(cli, "ImmichClient", _Api)
    from immy import trips
    monkeypatch.setattr(trips, "link_tags", lambda conn, links: db.setdefault("links", links))
    res = CliRunner().invoke(cli.app, ["tags", "model", "--match", "meta", "--config", str(cfg)])
    assert res.exit_code == 0, res.output
    assert "links" not in db and db["patterns"] == ["%meta%"]               # dry run
    res = CliRunner().invoke(cli.app, ["tags", "model", "--match", "meta", "--config", str(cfg), "--write"])
    assert res.exit_code == 0, res.output
    assert sorted(db["links"]) == [
        ("a1", "id:Gear/Camera/Ray-Ban Meta Smart Glasses 2", "Gear/Camera/Ray-Ban Meta Smart Glasses 2"),
        ("a2", "id:Gear/Camera/Ray-Ban Meta Smart Glasses 2", "Gear/Camera/Ray-Ban Meta Smart Glasses 2"),
        ("a3", "id:Gear/Camera/Meta Smart Glasses", "Gear/Camera/Meta Smart Glasses")]
