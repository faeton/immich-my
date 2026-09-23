"""`immy doctor` — each check in isolation, with fakes for the network."""

from __future__ import annotations

from pathlib import Path

from immy import doctor
from immy.config import Config, ImmichConfig, MediaConfig, MLConfig, PgConfig
from immy.immich import ImmichError


def _config(**kw) -> Config:
    base = dict(
        originals_root=None, immich=None, pg=None, media=None, ml=None,
        notes_filename=None, source=Path("/etc/immy.yml"),
    )
    return Config(**{**base, **kw})


def _statuses(checks):
    return {c.name: c.status for c in checks}


def test_unconfigured_sections_skip_rather_than_fail():
    checks = doctor.check_immich(_config()) + doctor.check_postgres(_config())
    assert {c.status for c in checks} == {doctor.SKIP}


def test_missing_binary_fails():
    got = _statuses(doctor.check_binaries(which=lambda n: None if n == "ffprobe" else f"/bin/{n}"))
    assert got["bin ffprobe"] == doctor.FAIL
    assert got["bin exiftool"] == doctor.OK


def test_paths(tmp_path):
    cfg = _config(
        originals_root=tmp_path, state_root=tmp_path / "nope",
        media=MediaConfig(host_root=str(tmp_path / "gone"), container_root="data"),
    )
    got = _statuses(doctor.check_paths(cfg))
    assert got["originals_root"] == doctor.OK
    assert got["state_root"] == doctor.FAIL
    assert got["sidecars_root"] == doctor.SKIP
    assert got["media host_root"] == doctor.WARN        # NAS path, absent from the Mac
    assert got["media container_root"] == doctor.FAIL   # not absolute


class _FakeClient:
    def __init__(self, responses):
        self.responses = responses

    def _request(self, method, path, body=None):
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value


def _immich_cfg():
    return _config(immich=ImmichConfig(url="http://imm", api_key="k", library_id="L"))


def test_immich_reachable_with_import_paths():
    client = _FakeClient({
        "/api/server/about": {"version": "2.1.0"},
        "/api/libraries/L": {"name": "ext", "importPaths": ["/originals"]},
    })
    got = _statuses(doctor.check_immich(_immich_cfg(), client_factory=lambda: client))
    assert got == {"immich api": doctor.OK, "immich library": doctor.OK}


def test_immich_library_without_import_paths_fails():
    client = _FakeClient({
        "/api/server/about": {"version": "2.1.0"},
        "/api/libraries/L": {"name": "ext", "importPaths": []},
    })
    got = _statuses(doctor.check_immich(_immich_cfg(), client_factory=lambda: client))
    assert got["immich library"] == doctor.FAIL


def test_immich_unreachable_stops_at_first_failure():
    client = _FakeClient({"/api/server/about": ImmichError("transport: refused")})
    checks = doctor.check_immich(_immich_cfg(), client_factory=lambda: client)
    assert [(c.name, c.status) for c in checks] == [("immich api", doctor.FAIL)]


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakePg:
    """Answers the handful of queries doctor issues."""

    def __init__(self, columns, dim="vector(512)"):
        self.columns, self.dim, self.closed = columns, dim, False

    def execute(self, sql, params=()):
        if "information_schema.columns" in sql:
            return _FakeCursor([(c,) for c in self.columns.get(params[0], ())])
        if "smart_search" in sql:
            return _FakeCursor([(self.dim,)])
        raise AssertionError(sql)

    def close(self):
        self.closed = True


def _pg_cfg(model="ViT-B-32__openai"):
    return _config(
        pg=PgConfig(host="db", port=5432, user="u", password="p", database="immich"),
        ml=MLConfig(clip_model=model),
    )


def test_postgres_schema_and_clip_dim_ok():
    fake = _FakePg(dict(doctor.DIRECT_WRITE_COLUMNS))
    got = _statuses(doctor.check_postgres(_pg_cfg(), connect=lambda cfg: fake))
    assert set(got.values()) == {doctor.OK}
    assert fake.closed


def test_postgres_renamed_column_and_wrong_dim_fail():
    columns = dict(doctor.DIRECT_WRITE_COLUMNS)
    columns["asset"] = tuple(c for c in columns["asset"] if c != "localDateTime")
    fake = _FakePg(columns, dim="vector(768)")
    got = _statuses(doctor.check_postgres(_pg_cfg(), connect=lambda cfg: fake))
    assert got["table asset"] == doctor.FAIL
    assert got["clip dim"] == doctor.FAIL
    assert got["table asset_exif"] == doctor.OK


def test_postgres_unreachable_fails_without_raising():
    def boom(cfg):
        raise OSError("refused")
    assert _statuses(doctor.check_postgres(_pg_cfg(), connect=boom)) == {"postgres": doctor.FAIL}


def test_ml_backend_coherence():
    got = _statuses(doctor.check_ml_endpoints(_config(ml=MLConfig(
        clip_backend="immich-ml", whisper_backend="qwen-asr",
    ))))
    assert got == {"ml clip": doctor.FAIL, "ml whisper": doctor.FAIL}


def test_run_all_never_raises():
    checks = doctor.run_all(_config(source=None))
    assert checks[0].name == "config" and checks[0].status == doctor.WARN
