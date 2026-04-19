from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from immy.cli import app
from immy import immich as immich_mod
from immy import promote as promote_mod


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

    def find_asset_id(self, original_file_name: str) -> str | None:
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


def test_promote_creates_album_with_description_from_notes(
    config_file, dji_ready, monkeypatch
):
    _, originals = config_file
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
