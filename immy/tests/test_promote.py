from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from immy.cli import app
from immy.config import Config
from immy.heartbeat import heartbeat_path
from immy import immich as immich_mod
from immy import promote as promote_mod
from immy.pg import LibraryInfo


FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


class FakeClient:
    """Records every call; no HTTP. Tests inject via monkeypatch of ImmichClient."""

    def __init__(
        self,
        *,
        indexed: set[str] | None = None,
        existing_albums: list[dict] | None = None,
        url: str = "",
        api_key: str = "",
    ):
        self.indexed = indexed if indexed is not None else set()
        self.scans: list[str] = []
        self.stacks: list[tuple[str, list[str]]] = []
        self.existing_albums = list(existing_albums or [])
        self.albums_created: list[dict] = []
        self.albums_updated: list[tuple[str, dict]] = []
        self.album_assets: list[tuple[str, list[str]]] = []

    def scan_library(self, library_id: str) -> None:
        self.scans.append(library_id)

    def regenerate_thumbnails(self, asset_ids: list[str]) -> None:
        self.thumb_regens = getattr(self, "thumb_regens", [])
        self.thumb_regens.append(list(asset_ids))

    def find_asset_id(
        self,
        original_file_name: str,
        *,
        original_path_suffix: str | None = None,
    ) -> str | None:
        return f"id-{original_file_name}" if original_file_name in self.indexed else None

    def create_stack(self, primary_asset_id: str, other_asset_ids: list[str]) -> str | None:
        self.stacks.append((primary_asset_id, other_asset_ids))
        return "stack-1"

    # Album surface ---------------------------------------------------------

    def find_album_by_name(self, name: str) -> dict | None:
        for album in self.existing_albums:
            if album.get("albumName") == name:
                return album
        return None

    def create_album(self, name, *, description=None, asset_ids=None):
        record = {
            "name": name,
            "description": description,
            "asset_ids": list(asset_ids or []),
        }
        self.albums_created.append(record)
        return f"album-{name}"

    def update_album(self, album_id, *, description=None):
        self.albums_updated.append((album_id, {"description": description}))

    def add_assets_to_album(self, album_id, asset_ids):
        self.album_assets.append((album_id, list(asset_ids)))
        return [{"id": aid, "success": True} for aid in asset_ids]

    # Tag surface -----------------------------------------------------------

    def upsert_tags(self, names):
        self.tags_upserted = getattr(self, "tags_upserted", [])
        self.tags_upserted.append(list(names))
        return {n: f"tag-{n}" for n in names}

    def tag_assets(self, tag_id, asset_ids):
        self.assets_tagged = getattr(self, "assets_tagged", [])
        self.assets_tagged.append((tag_id, list(asset_ids)))
        return [{"id": aid, "success": True} for aid in asset_ids]


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    originals = tmp_path / "originals-test"
    originals.mkdir()
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        yaml.safe_dump({
            "originals_root": str(originals),
            "immich": {
                "url": "http://fake",
                "api_key": "k",
                "library_id": "lib-1",
            },
        })
    )
    monkeypatch.setenv("IMMY_CONFIG", str(cfg))
    return cfg, originals


@pytest.fixture
def dji_ready(tmp_path: Path) -> Path:
    """DJI fixture after a successful audit — no HIGH findings pending."""
    target = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", target)
    runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    return target


@pytest.fixture
def insta360_ready(tmp_path: Path) -> Path:
    target = tmp_path / "insta360-pair"
    shutil.copytree(FIXTURES / "insta360-pair", target)
    runner.invoke(app, ["audit", str(target), "--write", "--auto"])
    return target


def test_promote_dry_run_no_rsync_no_api(config_file, dji_ready, monkeypatch):
    _, originals = config_file
    fake = FakeClient()
    monkeypatch.setattr(immich_mod, "ImmichClient", lambda **kw: fake)

    result = runner.invoke(app, ["promote", str(dji_ready), "--dry-run"])
    assert result.exit_code == 0, result.stdout
    # dry-run → no files copied
    assert not (originals / "dji-srt-pair" / "DJI_0001.JPG").exists()
    assert fake.scans == []
    assert fake.stacks == []


def test_promote_rsyncs_and_triggers_scan(config_file, dji_ready, monkeypatch):
    _, originals = config_file
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout

    assert (originals / "dji-srt-pair" / "DJI_0001.JPG").is_file()
    assert (originals / "dji-srt-pair" / "DJI_0001.xmp").is_file()
    # .audit/ excluded
    assert not (originals / "dji-srt-pair" / ".audit").exists()
    assert fake.scans == ["lib-1"]


def test_rsync_two_pass_never_plain_append(tmp_path, monkeypatch):
    # promote rsync runs two passes: a sidecar-correctness pass (NO
    # --append-verify) then the bulk media pass (--append-verify when
    # supported). Plain --append must never appear; --inplace/--partial always.
    calls: list = []

    def fake_run(args):
        calls.append(args)
        return MagicMock(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(promote_mod, "_run_streaming", fake_run)
    src = tmp_path / "trip"
    src.mkdir()
    promote_mod.rsync(src, tmp_path / "dest", dry_run=True)

    assert len(calls) == 2
    sidecar, bulk = calls
    # Identify the sidecar pass by its include filter.
    assert "--include=*/" in sidecar
    assert any(a.startswith("--include=*.srt") for a in sidecar)
    for args in calls:
        assert "--append" not in args      # never the unverified variant
        assert "--inplace" in args and "--partial" in args
    # Sidecar pass must NOT append-verify (skips same-size/shorter edits).
    assert "--append-verify" not in sidecar
    # Bulk pass uses it when the local rsync supports it.
    if promote_mod._rsync_supports("--append-verify"):
        assert "--append-verify" in bulk


def test_rsync_resyncs_edited_shorter_sidecar(tmp_path):
    # The bug this guards: --append-verify silently skips a transcript whose
    # new (source) copy is the same size or shorter than the stale dest, so an
    # edited/re-generated `.ru.srt` would never overwrite the NAS copy. The
    # sidecar pass must fix it. Real rsync to a local dest — no mocks.
    src = tmp_path / "trip"
    dst = tmp_path / "dest" / "trip"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    # stale, LONGER copy already on the "NAS"
    (dst / "VID_001.ru.srt").write_text("OLD wrong line 1\nOLD wrong line 2\nextra\n")
    # edited, SHORTER copy locally
    (src / "VID_001.ru.srt").write_text("new fixed\n")
    # an immutable media file alongside, to prove the bulk pass still copies
    (src / "VID_001.mp4").write_bytes(b"\x00" * 4096)

    promote_mod.rsync(src, dst, dry_run=False)

    assert (dst / "VID_001.ru.srt").read_text() == "new fixed\n"
    assert (dst / "VID_001.mp4").is_file()


def test_rsync_resyncs_samesize_samemtime_sidecar(tmp_path):
    # Hardest case: an edited transcript that is the SAME byte size AND has the
    # SAME mtime as the stale dest (e.g. a prior rsync preserved the mtime).
    # rsync's size+mtime quick-check would skip it; only --checksum on the
    # sidecar pass catches the content difference.
    import os
    src = tmp_path / "trip"
    dst = tmp_path / "dest" / "trip"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    (dst / "VID_001.ru.srt").write_text("AAAAAAAAAA\n")
    (src / "VID_001.ru.srt").write_text("BBBBBBBBBB\n")  # same length, diff content
    fixed_mtime = 1_700_000_000
    os.utime(dst / "VID_001.ru.srt", (fixed_mtime, fixed_mtime))
    os.utime(src / "VID_001.ru.srt", (fixed_mtime, fixed_mtime))

    promote_mod.rsync(src, dst, dry_run=False)

    assert (dst / "VID_001.ru.srt").read_text() == "BBBBBBBBBB\n"


def test_rsync_never_leaks_audit_dir(tmp_path):
    # `.audit/` must stay excluded in BOTH passes (the sidecar pass includes
    # *.json, which would otherwise pull `.audit/*.json`).
    src = tmp_path / "trip"
    dst = tmp_path / "dest" / "trip"
    (src / ".audit").mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / ".audit" / "state.json").write_text("{}")
    (src / "VID_001.ru.srt").write_text("hi\n")
    (src / "VID_001.mp4").write_bytes(b"\x00" * 64)

    promote_mod.rsync(src, dst, dry_run=False)

    assert not (dst / ".audit").exists()
    assert (dst / "VID_001.ru.srt").is_file()


def test_promote_interrupt_stops_without_traceback(config_file, dji_ready, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(promote_mod, "rsync", interrupted)

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 130, result.stdout
    assert "interrupted" in result.stdout
    assert "Traceback" not in result.stdout
    assert fake.scans == []
    assert fake.stacks == []
    assert not heartbeat_path(dji_ready).exists()


def test_promote_drains_offline_cache_when_pending(
    config_file, dji_ready, monkeypatch,
):
    """If `.audit/offline/<cs>.yml` entries exist, promote must try to
    sync them before the scan. When pg is unreachable, failure is soft
    (surfaced in summary) so the rsync step still runs."""
    from immy import offline as offline_mod
    from immy import process as process_mod
    from immy.pg import LibraryInfo

    # Stash an unsynced offline entry so the drain step has work to do.
    lib = LibraryInfo(id="lib-1", owner_id="owner-1", container_root="/x")
    sink = offline_mod.OfflineSink(dji_ready, lib)
    process_mod.process_trip(dji_ready, None, lib, sink=sink)

    # pg_mod.connect raises — simulates tailnet down. promote must not
    # crash, and must surface the failure in its summary.
    from immy import promote as promote_mod
    monkeypatch.setattr(
        "immy.promote.pg_mod.connect",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("tailnet down")),
    )
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    # Rich wraps long lines, so check for the tokens individually rather
    # than a composite phrase.
    flat = result.stdout.replace("\n", " ").replace("  ", " ")
    # Proves the drain step ran and surfaced the failure instead of
    # silently letting the promote continue into a split-brain state.
    assert "offline-sync" in flat, flat
    assert "1 pending" in flat, flat


def test_promote_aliases_push_and_pub_work(config_file, dji_ready, monkeypatch):
    _, originals = config_file
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    result = runner.invoke(app, ["push", str(dji_ready), "--dry-run"])
    assert result.exit_code == 0, result.stdout
    result = runner.invoke(app, ["pub", str(dji_ready), "--dry-run"])
    assert result.exit_code == 0, result.stdout


def test_promote_refuses_on_pending_high(config_file, tmp_path, monkeypatch):
    _, originals = config_file
    # Copy fixture WITHOUT running audit → HIGH findings stay pending.
    folder = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", folder)

    result = runner.invoke(app, ["promote", str(folder)])
    assert result.exit_code == 1, result.stdout
    assert "HIGH finding" in result.stdout
    assert not (originals / "dji-srt-pair" / "DJI_0001.JPG").exists()


def test_promote_force_overrides_pending_high(config_file, tmp_path, monkeypatch):
    _, originals = config_file
    folder = tmp_path / "dji-srt-pair"
    shutil.copytree(FIXTURES / "dji-srt-pair", folder)
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    result = runner.invoke(app, ["promote", str(folder), "--force"])
    assert result.exit_code == 0, result.stdout
    assert (originals / "dji-srt-pair" / "DJI_0001.JPG").is_file()


def test_promote_insta360_pair_triggers_stack(config_file, insta360_ready, monkeypatch):
    _, originals = config_file
    fake = FakeClient(indexed={
        "VID_20240101_120000_00_001.insv",
        "LRV_20240101_120000_01_001.lrv",
    })
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    # Collapse the wait_for_asset poll so the test is fast.
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(insta360_ready)])
    assert result.exit_code == 0, result.stdout
    assert fake.scans == ["lib-1"]
    assert len(fake.stacks) == 1
    primary, others = fake.stacks[0]
    # .lrv is the primary; .insv is the child.
    assert primary == "id-LRV_20240101_120000_01_001.lrv"
    assert others == ["id-VID_20240101_120000_00_001.insv"]


def test_promote_no_immich_creds_rsyncs_only(tmp_path, dji_ready, monkeypatch):
    # Config with originals_root but NO immich section.
    originals = tmp_path / "originals-only"
    cfg = tmp_path / "no-immich.yml"
    cfg.write_text(yaml.safe_dump({"originals_root": str(originals)}))
    monkeypatch.setenv("IMMY_CONFIG", str(cfg))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    assert (originals / "dji-srt-pair" / "DJI_0001.JPG").is_file()
    assert "no immich creds" in result.stdout


def test_promote_without_config_errors(tmp_path, dji_ready, monkeypatch):
    monkeypatch.setenv("IMMY_CONFIG", str(tmp_path / "missing.yml"))
    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 2, result.stdout
    assert "no originals_root" in result.stdout


def test_promote_excludes_audit_dir(config_file, dji_ready, monkeypatch):
    _, originals = config_file
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    # Source has .audit/state.yml from the audit fixture.
    assert (dji_ready / ".audit" / "state.yml").is_file()
    # Destination should NOT — machine state stays on the Mac.
    assert not (originals / "dji-srt-pair" / ".audit").exists()


def test_build_plan_ignores_staged_derivatives_for_pending_high(
    config_file, dji_ready, monkeypatch,
):
    cfg_path, originals = config_file
    derived = (
        dji_ready
        / ".audit"
        / "derivatives"
        / "thumbs"
        / "u"
        / "ab"
        / "cd"
        / "generated_preview.jpeg"
    )
    derived.parent.mkdir(parents=True, exist_ok=True)
    derived.write_bytes(b"x")

    cfg = Config(
        originals_root=originals,
        immich=None,
        pg=None,
        media=None,
        ml=None,
        notes_filename=None,
        source=cfg_path,
    )
    plan = promote_mod.build_plan(dji_ready, cfg)
    assert plan.pending_high == 0


def test_build_plan_dedups_pending_high_like_audit(
    config_file, dji_ready, monkeypatch,
):
    """Regression: promote must dedup findings the SAME way `immy audit`
    does. Two HIGH rules claim the same file's GPS field; the first-
    registered winner is already applied. The deduped-out loser must NOT
    be counted as pending — otherwise promote refuses a folder audit
    considers clean and the trip is stranded as perpetually pending.
    """
    from immy.rules import Finding
    from immy.state import State, patch_hash

    cfg_path, originals = config_file
    cfg = Config(
        originals_root=originals, immich=None, pg=None, media=None, ml=None,
        notes_filename=None, source=cfg_path,
    )
    target = dji_ready / "DJI_0001.JPG"
    winner = Finding(rule="rule-a", confidence="high", path=target,
                     action="write_xmp", patch={"GPSLatitude": "1.0"})
    loser = Finding(rule="rule-b", confidence="high", path=target,
                    action="write_xmp", patch={"GPSLatitude": "2.0"})
    # build_plan calls evaluate(); return the two competitors regardless of rows.
    monkeypatch.setattr(promote_mod, "evaluate", lambda rows, folder: [winner, loser])

    # Mark only the winner applied in on-disk state.
    rel = "DJI_0001.JPG"
    state = State.load(dji_ready)
    state.mark_applied(rel, winner.rule, patch_hash(
        {"action": winner.action, "patch": winner.patch,
         "pair_with": str(winner.pair_with)}))
    state.save()

    plan = promote_mod.build_plan(dji_ready, cfg)
    # Without dedup the loser (rule-b, unapplied) would make this 1.
    assert plan.pending_high == 0


def test_promote_idempotent(config_file, dji_ready, monkeypatch):
    _, originals = config_file
    fake = FakeClient()
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)

    runner.invoke(app, ["promote", str(dji_ready)])
    mtime = (originals / "dji-srt-pair" / "DJI_0001.JPG").stat().st_mtime
    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    # rsync with no source changes → same mtime on destination.
    assert (originals / "dji-srt-pair" / "DJI_0001.JPG").stat().st_mtime == mtime
    # Scan still triggered (Immich needs to be told, even if no new files).
    assert len(fake.scans) == 2


# --- album sync (new in 2a post-2c) ----------------------------------------


def _indexed_set(folder: Path) -> set[str]:
    from immy.exif import iter_media
    return {p.name for p in iter_media(folder)}


def _enable_fake_album_pg(config_file: tuple[Path, Path], monkeypatch) -> MagicMock:
    cfg_path, _ = config_file
    data = yaml.safe_load(cfg_path.read_text()) or {}
    data["pg"] = {
        "host": "127.0.0.1", "port": 15432,
        "user": "postgres", "password": "x", "database": "immich",
    }
    cfg_path.write_text(yaml.safe_dump(data))

    fake_conn = MagicMock()
    fake_conn.closed = False
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.rowcount = 0
    cur.fetchall.return_value = [("asset-1",)]
    cur.fetchone.return_value = (0,)  # trashed_skipped count query
    fake_conn.cursor.return_value = cur
    monkeypatch.setattr(promote_mod.pg_mod, "connect", lambda cfg: fake_conn)
    monkeypatch.setattr(
        promote_mod.pg_mod,
        "fetch_library_info",
        lambda conn, lib_id: LibraryInfo(
            id=lib_id,
            owner_id="owner-1",
            container_root="/mnt/external/originals",
        ),
    )
    return fake_conn


def test_promote_creates_album_with_description_from_notes(
    config_file, dji_ready, monkeypatch
):
    _, originals = config_file
    _enable_fake_album_pg(config_file, monkeypatch)
    # Append a body to the notes file so `notes_body` returns something.
    notes = dji_ready / "README.md"
    text = notes.read_text()
    notes.write_text(text + "\nDrone flyover at sunset.\n")

    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout

    assert len(fake.albums_created) == 1
    created = fake.albums_created[0]
    assert created["name"] == "dji-srt-pair"
    assert "Drone flyover at sunset." in (created["description"] or "")
    assert created["asset_ids"]  # at least one asset attached


def test_promote_updates_existing_album(config_file, dji_ready, monkeypatch):
    _, originals = config_file
    _enable_fake_album_pg(config_file, monkeypatch)
    notes = dji_ready / "README.md"
    text = notes.read_text()
    notes.write_text(text + "\nNew body text.\n")

    existing = [{
        "id": "album-existing",
        "albumName": "dji-srt-pair",
        "description": "old description",
    }]
    fake = FakeClient(indexed=_indexed_set(dji_ready), existing_albums=existing)
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout

    assert fake.albums_created == []  # no new album
    assert fake.albums_updated  # description patched
    updated_id, body = fake.albums_updated[0]
    assert updated_id == "album-existing"
    assert "New body text." in body["description"]
    assert fake.album_assets  # assets added (idempotent on Immich side)


def test_rsync_derivatives_runs_remote_as_root(monkeypatch):
    """Remote thumbs dst → `--rsync-path=sudo rsync` (immich-owned dir, sudo
    NOPASSWD on n5). Local dst → no sudo."""
    seen = {}
    monkeypatch.setattr(
        promote_mod, "_run_streaming",
        lambda args: seen.setdefault("args", args),
    )
    promote_mod._rsync_derivatives(Path("/x/.audit/derivatives"), "n5:/mnt/flash/immich")
    assert "--rsync-path=sudo rsync" in seen["args"]
    assert seen["args"][-1] == "n5:/mnt/flash/immich"  # remote dst kept verbatim

    seen.clear()
    promote_mod._rsync_derivatives(Path("/x/.audit/derivatives"), "/mnt/flash/immich")
    assert "--rsync-path=sudo rsync" not in seen["args"]  # local → no sudo


def _sql_recording_pg(config_file, monkeypatch, *, trashed_skipped=0):
    """Fake pg whose cursor records every executed SQL string. fetchall →
    one asset id; fetchone → the trashed_skipped count. Also enables the
    `pg:` block in the config so `_sync_album` doesn't short-circuit."""
    cfg_path, _ = config_file
    data = yaml.safe_load(cfg_path.read_text()) or {}
    data["pg"] = {"host": "127.0.0.1", "port": 15432,
                  "user": "postgres", "password": "x", "database": "immich"}
    cfg_path.write_text(yaml.safe_dump(data))
    executed: list[str] = []
    fake_conn = MagicMock()
    fake_conn.closed = False
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.rowcount = 1
    cur.fetchall.return_value = [("asset-1",)]
    cur.fetchone.return_value = (trashed_skipped,)
    cur.execute.side_effect = lambda sql, params=None: executed.append(" ".join(sql.split()))
    fake_conn.cursor.return_value = cur
    monkeypatch.setattr(promote_mod.pg_mod, "connect", lambda cfg: fake_conn)
    monkeypatch.setattr(
        promote_mod.pg_mod, "fetch_library_info",
        lambda conn, lib_id: LibraryInfo(
            id=lib_id, owner_id="o", container_root="/mnt/external/originals"),
    )
    return executed


def test_default_promote_untrashes_offline_autotrash(config_file, dji_ready, monkeypatch):
    """DEFAULT (no --resurrect-deleted): the un-trash UPDATE clears BOTH
    isOffline and deletedAt, gated on isOffline=true (the offline-auto-trash
    signature) — so the never-promoted backlog lands without the flag, while
    online soft-deletes (deletedAt-only) are untouched."""
    executed = _sql_recording_pg(config_file, monkeypatch)
    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    updates = [s for s in executed if s.startswith("UPDATE asset")]
    assert len(updates) == 1
    u = updates[0]
    assert '"isOffline" = false' in u and '"deletedAt" = NULL' in u
    assert '"isOffline" = true' in u            # gated on the offline signature
    assert '"deletedAt" IS NOT NULL' not in u   # NOT the resurrect-all form


def test_resurrect_deleted_untrashes_everything(config_file, dji_ready, monkeypatch):
    """--resurrect-deleted broadens the UPDATE to also include online
    soft-deletes (deletedAt IS NOT NULL)."""
    executed = _sql_recording_pg(config_file, monkeypatch)
    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready), "--resurrect-deleted"])
    assert result.exit_code == 0, result.stdout
    u = [s for s in executed if s.startswith("UPDATE asset")][0]
    assert '"deletedAt" IS NOT NULL' in u  # online soft-deletes included


def test_promote_warns_on_trashed_skipped(config_file, dji_ready, monkeypatch):
    """Residual online-trashed assets are surfaced, never silent."""
    _sql_recording_pg(config_file, monkeypatch, trashed_skipped=4)
    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    assert "4 asset(s)" in result.stdout and "--resurrect-deleted" in result.stdout


def test_promote_into_album_merges_into_existing(config_file, dji_ready, monkeypatch):
    """`--into-album X` adds THIS trip's assets (resolved from its own path) to
    the existing album X, creates no folder-named album, and does NOT clobber
    X's description with the source trip's notes."""
    _enable_fake_album_pg(config_file, monkeypatch)
    notes = dji_ready / "README.md"
    notes.write_text(notes.read_text() + "\nSource trip body — must NOT leak.\n")

    existing = [{
        "id": "album-anya",
        "albumName": "2024-12-anya-beach-photoshop",
        "description": "anya's own description",
    }]
    fake = FakeClient(indexed=_indexed_set(dji_ready), existing_albums=existing)
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(
        app, ["promote", str(dji_ready),
              "--into-album", "2024-12-anya-beach-photoshop"],
    )
    assert result.exit_code == 0, result.stdout
    assert fake.albums_created == []                 # no folder-named album
    assert fake.albums_updated == []                 # description left alone
    assert fake.album_assets                          # assets added to target
    target_id, ids = fake.album_assets[0]
    assert target_id == "album-anya" and ids          # the existing album


def test_promote_applies_tags(config_file, dji_ready, monkeypatch):
    """`--tag a --tag b` upserts both tags and attaches each to the trip's
    assets."""
    _enable_fake_album_pg(config_file, monkeypatch)
    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(
        app, ["promote", str(dji_ready),
              "--tag", "post-edited", "--tag", "with-anya"],
    )
    assert result.exit_code == 0, result.stdout
    assert fake.tags_upserted == [["post-edited", "with-anya"]]
    tagged = {tid for tid, _ in fake.assets_tagged}
    assert tagged == {"tag-post-edited", "tag-with-anya"}
    for _, ids in fake.assets_tagged:
        assert ids  # asset ids attached to each tag


def test_promote_no_tags_leaves_tag_surface_untouched(config_file, dji_ready, monkeypatch):
    """No --tag → no tag API calls (byte-identical to the pre-feature path)."""
    _enable_fake_album_pg(config_file, monkeypatch)
    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    assert not hasattr(fake, "tags_upserted")
    assert not hasattr(fake, "assets_tagged")


def test_promote_repairs_thumbnails_for_brought_online_assets(
    config_file, dji_ready, monkeypatch
):
    """Assets registered while offline keep broken thumbs after the files
    land unless promote re-queues regeneration. The fake pg returns asset
    ids needing repair → promote must call regenerate_thumbnails for them."""
    _enable_fake_album_pg(config_file, monkeypatch)
    fake = FakeClient(indexed=_indexed_set(dji_ready))
    monkeypatch.setattr("immy.cli.ImmichClient", lambda **kw: fake)
    monkeypatch.setattr(promote_mod, "wait_for_asset", lambda c, n, **kw: c.find_asset_id(n))

    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0, result.stdout
    assert getattr(fake, "thumb_regens", []) == [["asset-1"]]


def test_promote_skips_album_when_no_immich_creds(tmp_path, dji_ready, monkeypatch):
    originals = tmp_path / "originals-noim"
    cfg = tmp_path / "no-immich.yml"
    cfg.write_text(yaml.safe_dump({"originals_root": str(originals)}))
    monkeypatch.setenv("IMMY_CONFIG", str(cfg))
    result = runner.invoke(app, ["promote", str(dji_ready)])
    assert result.exit_code == 0
    assert "no immich creds" in result.stdout
    # No album line in output (status "skipped" hidden by CLI).
    assert "album " not in result.stdout
