"""Transaction + journal atomicity for `process_trip` (audit 2026-10, items 5/6).

The fake connection below models the Postgres behaviour the bugs hinge on:

- a failed statement leaves the transaction ABORTED (INERROR); every later
  statement fails until ROLLBACK / ROLLBACK TO SAVEPOINT;
- `COMMIT` on an aborted transaction does NOT raise — the server answers
  `ROLLBACK` and psycopg's `conn.commit()` returns normally, silently
  discarding the whole transaction;
- `conn.transaction()` inside an open transaction is a SAVEPOINT (psycopg 3):
  an exception inside the block rolls back to it and the outer transaction
  is healthy again; at IDLE it is an outer BEGIN … COMMIT block.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import pq

from immy import journal as journal_mod
from immy import offline as offline_mod
from immy import process as process_mod
from immy.journal import Journal
from immy.pg import LibraryInfo


FIXTURES = Path(__file__).parent / "fixtures"
LIB = LibraryInfo(id="lib-1", owner_id="owner-1",
                  container_root="/mnt/external/originals")

IDLE = pq.TransactionStatus.IDLE
INTRANS = pq.TransactionStatus.INTRANS
INERROR = pq.TransactionStatus.INERROR


class _FakeCursor:
    def __init__(self, conn: "FakePgConn") -> None:
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._row = self.conn._exec(sql, params or {})
        return self

    def fetchone(self):
        row, self._row = self._row, None
        return row


class _FakeTransaction:
    """psycopg 3 `Connection.transaction()` semantics (see
    psycopg/transaction.py `_push_savepoint` / `_get_*_commands`)."""

    def __init__(self, conn: "FakePgConn") -> None:
        self.conn = conn

    def __enter__(self):
        self.outer = self.conn.status == IDLE
        if self.outer:
            self.conn.status = INTRANS
        else:
            self.mark = len(self.conn.pending)
        self.conn.savepoints_opened += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            if self.conn.status == INERROR:
                # RELEASE in an aborted transaction errors out.
                raise psycopg.errors.InFailedSqlTransaction(
                    "current transaction is aborted")
            if self.outer:
                self.conn.commit()
            return False
        if self.outer:
            self.conn.rollback()
        else:
            # ROLLBACK TO SAVEPOINT: works from INERROR, heals the txn.
            del self.conn.pending[self.mark:]
            self.conn.status = INTRANS
        return False


class FakePgConn:
    def __init__(self, *, existing: dict[bytes, str] | None = None,
                 fail_on: tuple[str, ...] = (),
                 commit_error: Exception | None = None,
                 smart_search_dim: int = 4) -> None:
        self.status = IDLE
        self.pending: list[tuple[str, dict]] = []
        self.committed: list[tuple[str, dict]] = []
        # checksum → asset id of rows already in the DB before this run.
        self.rows: dict[bytes, str] = dict(existing or {})
        self.fail_on = fail_on
        self.commit_error = commit_error
        self.smart_search_dim = smart_search_dim
        self.closed = False
        self.savepoints_opened = 0
        self.commits = 0

    @property
    def info(self):
        # psycopg: conn.info.transaction_status
        return SimpleNamespace(transaction_status=self.status)

    def cursor(self):
        return _FakeCursor(self)

    def execute(self, sql, params=None):
        cur = _FakeCursor(self)
        return cur.execute(sql, params)

    def transaction(self):
        return _FakeTransaction(self)

    def _exec(self, sql: str, params: dict):
        if self.status == INERROR:
            raise psycopg.errors.InFailedSqlTransaction(
                "current transaction is aborted, commands ignored until end "
                "of transaction block")
        if self.status == IDLE:
            self.status = INTRANS
        for frag in self.fail_on:
            if frag in sql:
                self.status = INERROR
                raise psycopg.errors.InvalidTextRepresentation(
                    f"simulated failure on {frag!r}")
        if "format_type" in sql:
            return (f"vector({self.smart_search_dim})",)
        if "INSERT INTO asset (" in sql:
            if params["checksum"] in self._visible_rows():
                return None  # ON CONFLICT DO NOTHING
            self.pending.append((sql, dict(params)))
            return (params["id"],)
        if sql.lstrip().startswith("SELECT id FROM asset"):
            found = self._visible_rows().get(params["checksum"])
            return (found,) if found else None
        if "SELECT description" in sql:
            return None
        self.pending.append((sql, dict(params) if isinstance(params, dict)
                             else {"args": params}))
        return None

    def _visible_rows(self) -> dict[bytes, str]:
        rows = dict(self.rows)
        for sql, p in self.pending:
            if "INSERT INTO asset (" in sql:
                rows[p["checksum"]] = p["id"]
        return rows

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            err, self.commit_error = self.commit_error, None
            raise err
        if self.status == INERROR:
            # Server turns COMMIT into ROLLBACK; psycopg does not raise.
            self.pending.clear()
        else:
            self.committed.extend(self.pending)
            self.rows = self._visible_rows()
            self.pending.clear()
        self.status = IDLE

    def rollback(self):
        self.pending.clear()
        self.status = IDLE

    def close(self):
        self.closed = True

    def committed_sql(self, fragment: str) -> list[dict]:
        return [p for s, p in self.committed if fragment in s]


# --- helpers ---------------------------------------------------------------


def _trip(tmp_path: Path) -> Path:
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    return target


def _cs(trip: Path) -> bytes:
    cpath = process_mod.container_path_for(
        trip / "DJI_0001.JPG", trip, LIB.container_root)
    return process_mod.path_checksum(cpath)


def _fake_derivative(preview_path: Path):
    from immy.derivatives import DerivativeFile, DerivativeResult
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(b"fake preview")
    return DerivativeResult(
        files=[DerivativeFile(
            kind="preview", staged_path=preview_path,
            relative_path="thumbs/owner-1/aa/bb/id_preview.jpeg",
            is_progressive=True, is_transparent=False,
        )],
        width=4000, height=3000,
    )


@pytest.fixture
def ml_stubs(tmp_path, monkeypatch):
    calls = {"derivatives": 0, "clip": 0}

    def _compute(**kw):
        calls["derivatives"] += 1
        return _fake_derivative(
            tmp_path / "out" / f"{kw['asset_id']}_preview.jpeg")

    def _embed(path, **kw):
        calls["clip"] += 1
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(
        "immy.process.derivatives_mod.compute_for_asset", _compute)
    monkeypatch.setattr("immy.process.clip_mod.embed", _embed)
    return calls


# --- 1. a failed enricher statement must not poison the asset --------------


def test_clip_sql_failure_is_contained_and_not_journaled(tmp_path, ml_stubs):
    """The smart_search upsert fails. Without a savepoint the txn aborts and
    the per-asset COMMIT silently rolls back the asset row too, while the
    journal already claims ingest + derivatives. With a per-enricher
    savepoint the asset (and its dims) commit, and CLIP is not journaled."""
    trip = _trip(tmp_path)
    conn = FakePgConn(fail_on=("INSERT INTO smart_search",))

    results = process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)

    assert results[0].clip_embedded is False
    inserted = conn.committed_sql("INSERT INTO asset (")
    assert len(inserted) == 1, "asset row was rolled back with the failed CLIP"
    assert conn.committed_sql("UPDATE asset SET width"), "dims rolled back"

    cs = _cs(trip).hex()
    j = Journal.load(trip)
    assert j.is_done(cs, "ingest", "v1")
    assert j.is_done(cs, "derivatives", journal_mod.DERIVATIVES_VERSION)
    assert not j.get(cs, "clip"), "journal claims CLIP that never committed"


def test_derivative_sql_failure_rolls_back_only_that_phase(tmp_path, ml_stubs):
    trip = _trip(tmp_path)
    conn = FakePgConn(fail_on=("UPDATE asset SET width",))

    results = process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True)

    assert results[0].derivatives is None
    assert len(conn.committed_sql("INSERT INTO asset (")) == 1
    cs = _cs(trip).hex()
    j = Journal.load(trip)
    assert j.is_done(cs, "ingest", "v1")
    assert not j.get(cs, "derivatives")


# --- 2. journal durable only after the asset's commit ----------------------


def test_commit_failure_leaves_no_journal_marks(tmp_path, ml_stubs):
    """The commit-failure branch used to flush the journal it had just
    marked — ghost entries for rows that never landed. The asset must be
    reported failed (exception naming the file) and the journal untouched."""
    trip = _trip(tmp_path)
    conn = FakePgConn(commit_error=psycopg.errors.SerializationFailure("boom"))

    with pytest.raises(Exception) as ei:
        process_mod.process_trip(
            trip, conn, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)
    assert "DJI_0001.JPG" in str(ei.value)

    j = Journal.load(trip)
    assert j.entries == {}, f"journal claims rolled-back work: {j.entries}"


def test_aborted_transaction_fails_loud_instead_of_silent_rollback(
    tmp_path, ml_stubs, monkeypatch,
):
    """Backstop: if any statement outside a savepoint aborted the txn and was
    swallowed, `PgSink.commit` must raise instead of letting COMMIT silently
    turn into ROLLBACK — and the journal must not record the asset."""
    trip = _trip(tmp_path)
    conn = FakePgConn()

    def _poisoning_record(self, asset_id, derivatives):
        # A swallowed, non-savepointed failure: txn is now INERROR.
        try:
            self.conn.execute("UPDATE poison SET x = 1")
        except psycopg.Error:
            pass

    conn.fail_on = ("UPDATE poison",)
    monkeypatch.setattr(offline_mod.PgSink, "record_derivatives",
                        _poisoning_record)

    with pytest.raises(Exception) as ei:
        process_mod.process_trip(trip, conn, LIB, compute_derivatives=True)
    assert "DJI_0001.JPG" in str(ei.value)
    assert conn.committed == []
    assert Journal.load(trip).entries == {}


def test_pgsink_commit_raises_on_aborted_transaction():
    conn = FakePgConn()
    conn.status = INERROR
    sink = offline_mod.PgSink(conn)
    with pytest.raises(offline_mod.TransactionAborted):
        sink.commit()
    assert conn.commits == 0, "COMMIT must not be sent on an aborted txn"


def test_journal_marks_promoted_after_successful_commit(tmp_path, ml_stubs):
    trip = _trip(tmp_path)
    conn = FakePgConn()
    process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)
    cs = _cs(trip).hex()
    j = Journal.load(trip)
    assert j.is_done(cs, "ingest", "v1")
    assert j.is_done(cs, "clip", journal_mod.clip_version(
        process_mod.clip_mod.DEFAULT_MODEL))
    assert len(conn.committed_sql("INSERT INTO smart_search")) == 1


# --- 3. DB-resolved asset id wins over the journal's ------------------------


def test_db_asset_id_wins_over_stale_journal_id(tmp_path, ml_stubs):
    trip = _trip(tmp_path)
    cs = _cs(trip)
    j = Journal.load(trip)
    j.mark_done(cs.hex(), "ingest", "v1", meta={"asset_id": "stale-journal-id"})
    j.flush()
    conn = FakePgConn(existing={cs: "db-row-id"})
    msgs: list[str] = []

    results = process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True,
        progress=msgs.append)

    assert results[0].asset_id == "db-row-id"
    upserts = conn.committed_sql("INSERT INTO smart_search")
    assert upserts and upserts[0]["asset_id"] == "db-row-id"
    assert any("stale-journal-id" in m and "db-row-id" in m for m in msgs), msgs
    j2 = Journal.load(trip)
    assert j2.get(cs.hex(), "ingest")["meta"]["asset_id"] == "db-row-id"


def test_journal_id_used_when_sink_cannot_resolve_row(tmp_path, ml_stubs,
                                                      monkeypatch):
    """No DB row resolvable (conflict but SELECT finds nothing) → keep the
    journal's recorded id, as before."""
    trip = _trip(tmp_path)
    cs = _cs(trip)
    j = Journal.load(trip)
    j.mark_done(cs.hex(), "ingest", "v1", meta={"asset_id": "journal-id"})
    j.flush()
    monkeypatch.setattr(offline_mod.PgSink, "insert_asset_and_exif",
                        lambda self, asset, exif: False)
    monkeypatch.setattr(offline_mod.PgSink, "existing_asset_id",
                        lambda self, *a: None)

    results = process_mod.process_trip(trip, FakePgConn(), LIB)
    assert results[0].asset_id == "journal-id"


# --- 4. a (re)inserted row invalidates cached later phases ------------------


def test_reinserted_row_does_not_trust_cached_phases(tmp_path, ml_stubs):
    """Journal says derivatives + CLIP done, but the asset row did not exist
    (it was rolled back / deleted) and this run inserted it. The cached
    phases describe a row that is gone; they must re-run."""
    trip = _trip(tmp_path)
    conn = FakePgConn()
    process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)
    assert ml_stubs == {"derivatives": 1, "clip": 1}

    # Same journal + staged files, but the DB lost the row.
    conn2 = FakePgConn()
    results = process_mod.process_trip(
        trip, conn2, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)

    assert results[0].inserted is True
    assert ml_stubs == {"derivatives": 2, "clip": 2}
    assert len(conn2.committed_sql("INSERT INTO smart_search")) == 1


def test_existing_row_keeps_cached_phases(tmp_path, ml_stubs):
    trip = _trip(tmp_path)
    conn = FakePgConn()
    process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)

    conn2 = FakePgConn(existing=dict(conn.rows))
    results = process_mod.process_trip(
        trip, conn2, LIB, compute_derivatives=True, compute_clip=True, allow_mlx_clip=True)
    assert results[0].inserted is False
    assert results[0].clip_embedded is True
    assert ml_stubs == {"derivatives": 1, "clip": 1}


# --- 5. parallel caption pool -------------------------------------------------


def test_caption_pool_write_failure_contained(tmp_path, ml_stubs, monkeypatch):
    from immy import captions as captions_mod
    trip = _trip(tmp_path)
    conn = FakePgConn(fail_on=("UPDATE asset_exif SET description",))
    monkeypatch.setattr(
        "immy.process.captions_mod.caption",
        lambda media, *, config, preview=None, context=None: SimpleNamespace(
            text="a caption", model=config.model,
            prompt_tokens=1, completion_tokens=1))
    cfg = captions_mod.CaptionerConfig(model="m1",
                                       endpoint="http://example.invalid/v1")

    results = process_mod.process_trip(
        trip, conn, LIB, compute_derivatives=True, compute_captions=True,
        captioner_config=cfg, caption_workers=2)

    assert results[0].caption is None
    assert len(conn.committed_sql("INSERT INTO asset (")) == 1
    cs = _cs(trip).hex()
    assert not Journal.load(trip).get(cs, "caption")


def test_caption_pool_commit_failure_not_journaled(tmp_path, ml_stubs,
                                                  monkeypatch):
    from immy import captions as captions_mod
    trip = _trip(tmp_path)
    conn = FakePgConn()
    monkeypatch.setattr(
        "immy.process.captions_mod.caption",
        lambda media, *, config, preview=None, context=None: SimpleNamespace(
            text="a caption", model=config.model,
            prompt_tokens=1, completion_tokens=1))
    real_commit = conn.commit

    def _commit():
        # First commit (the sequential pass) succeeds; the pool's fails.
        if conn.commits >= 1:
            conn.commits += 1
            raise psycopg.errors.SerializationFailure("boom")
        real_commit()
    conn.commit = _commit
    cfg = captions_mod.CaptionerConfig(model="m1",
                                       endpoint="http://example.invalid/v1")

    with pytest.raises(Exception, match="DJI_0001.JPG"):
        process_mod.process_trip(
            trip, conn, LIB, compute_derivatives=True, compute_captions=True,
            captioner_config=cfg, caption_workers=2)
    cs = _cs(trip).hex()
    j = Journal.load(trip)
    assert j.is_done(cs, "ingest", "v1")
    assert not j.get(cs, "caption")


# --- 6. offline sink: journal only after the cache entry is on disk ----------


def test_offline_failed_phase_not_journaled(tmp_path, ml_stubs, monkeypatch):
    trip = _trip(tmp_path)
    sink = offline_mod.OfflineSink(trip, LIB, clip_dim=4)

    def _boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(offline_mod.OfflineSink, "upsert_clip", _boom)

    process_mod.process_trip(
        trip, None, LIB, sink=sink, compute_derivatives=True,
        compute_clip=True, allow_mlx_clip=True)

    cs = _cs(trip).hex()
    j = Journal.load(trip)
    assert j.is_done(cs, "derivatives", journal_mod.DERIVATIVES_VERSION)
    assert not j.get(cs, "clip")
    entry = offline_mod._load_entry(offline_mod.offline_dir(trip) / f"{cs}.yml")
    assert entry["derivatives"], "journal claims derivatives the cache lacks"
