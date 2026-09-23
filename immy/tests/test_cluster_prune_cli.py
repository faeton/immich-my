"""`immy cluster --apply --prune` end to end, with fake Postgres + Immich."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from typer.testing import CliRunner

from immy import cli

runner = CliRunner()
T0 = datetime(2025, 6, 1, 10, 0)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)

    def close(self):
        pass


class _Immich:
    """Albums in memory; records every call."""

    albums: dict[str, dict] = {}
    calls: list[tuple] = []

    def __init__(self, **kw):
        pass

    def _request(self, method, path, body=None):
        assert (method, path) == ("GET", "/api/albums")
        return [
            {"id": aid, "albumName": a["name"], "description": a["description"]}
            for aid, a in self.albums.items()
        ]

    def create_album(self, name, *, description, asset_ids):
        aid = f"album{len(self.albums)}"
        self.albums[aid] = {"name": name, "description": description, "assets": set(asset_ids)}
        return aid

    def update_album(self, album_id, *, description):
        self.albums[album_id]["description"] = description

    def add_assets_to_album(self, album_id, ids):
        album = self.albums[album_id]["assets"]
        out = [{"id": i, "success": i not in album} for i in ids]
        album.update(ids)
        return out

    def remove_assets_from_album(self, album_id, ids):
        self.calls.append(("remove", album_id, tuple(ids)))
        album = self.albums[album_id]["assets"]
        out = [{"id": i, "success": i in album} for i in ids]
        album.difference_update(ids)
        return out


def _rows(spec):
    """spec: [(asset_id, hours_after_T0, lat)] — one geo event per lat."""
    return [(a, T0 + timedelta(hours=h), lat, 15.0, "Split", "Croatia") for a, h, lat in spec]


def _run(monkeypatch, tmp_path, rows, *extra):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        f"state_root: {tmp_path / 'state'}\n"
        "immich: {url: http://imm, api_key: k, library_id: L}\n"
        "pg: {host: db, user: u, password: p, database: immich}\n"
    )
    monkeypatch.setattr(cli.pg_mod, "connect", lambda _cfg: _Conn(rows), raising=False)
    monkeypatch.setattr(cli, "ImmichClient", _Immich)
    return runner.invoke(cli.app, [
        "cluster", "--apply", "--min-assets", "1", "--config", str(cfg), *extra,
    ])


def test_prune_moves_an_asset_out_of_its_old_album_but_keeps_manual_additions(
    monkeypatch, tmp_path,
):
    _Immich.albums, _Immich.calls = {}, []
    first = _rows([("a", 0, 45.0), ("b", 0.1, 45.0), ("c", 0.2, 45.0)])
    result = _run(monkeypatch, tmp_path, first)
    assert result.exit_code == 0, result.output
    (album_id,) = _Immich.albums
    _Immich.albums[album_id]["assets"].add("manual")      # user adds a photo by hand

    # "c" is re-dated a day later → its own event; the first album should lose it.
    second = _rows([("a", 0, 45.0), ("b", 0.1, 45.0), ("c", 30, 45.0)])
    result = _run(monkeypatch, tmp_path, second, "--prune")
    assert result.exit_code == 0, result.output

    assert _Immich.albums[album_id]["assets"] == {"a", "b", "manual"}
    assert any("c" in albums["assets"] for aid, albums in _Immich.albums.items() if aid != album_id)
    ledger = json.loads((tmp_path / "state" / "cluster-ledger.json").read_text())["albums"]
    assert sorted(ledger.values()) == [["a", "b"], ["c"]]


def test_without_prune_the_stale_claim_is_remembered(monkeypatch, tmp_path):
    _Immich.albums, _Immich.calls = {}, []
    _run(monkeypatch, tmp_path, _rows([("a", 0, 45.0), ("c", 0.2, 45.0)]))
    (album_id,) = _Immich.albums
    _run(monkeypatch, tmp_path, _rows([("a", 0, 45.0), ("c", 30, 45.0)]))
    assert _Immich.calls == []
    assert "c" in _Immich.albums[album_id]["assets"]      # not pruned…

    result = _run(monkeypatch, tmp_path, _rows([("a", 0, 45.0), ("c", 30, 45.0)]), "--prune")
    assert result.exit_code == 0, result.output
    assert _Immich.albums[album_id]["assets"] == {"a"}    # …until asked
