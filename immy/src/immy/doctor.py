"""`immy doctor` — preflight checks before a long run.

Each check is independent and never raises: it returns a `Check` with a
status (`ok` / `warn` / `fail` / `skip`) and one line of detail. A section
that isn't configured is `skip`, not `fail` — `audit` runs with no config at
all, and a transcript-only NAS deployment has no `ml.clip_model`. Only a
configured thing that is broken is a failure.

Read-only throughout: no DB writes, no API calls that mutate, no files
created.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# Output dimension of the CLIP models Immich ships. `smart_search.embedding`
# is declared `vector(N)` for the configured model; embeddings of a different
# width fail the insert. Unknown names are reported, not judged.
CLIP_DIMS = {
    "ViT-B-32__openai": 512,
    "ViT-B-16__openai": 512,
    "ViT-L-14__openai": 768,
    "ViT-B-32__laion2b-s34b-b79k": 512,
    "ViT-B-32__laion2b_e16": 512,
    "ViT-L-14__laion2b-s32b-b82k": 768,
    "XLM-Roberta-Large-Vit-B-32": 512,
    "nllb-clip-base-siglip__v1": 768,
}

# Columns `process` writes directly (Phase Y). An Immich upgrade that renames
# one turns every insert into an error mid-run; better to find out here.
DIRECT_WRITE_COLUMNS = {
    "asset": (
        "ownerId", "libraryId", "deviceAssetId", "deviceId", "originalPath",
        "originalFileName", "fileCreatedAt", "fileModifiedAt", "localDateTime",
        "isExternal", "checksumAlgorithm",
    ),
    "asset_exif": (
        "assetId", "dateTimeOriginal", "modifyDate", "timeZone",
        "exifImageWidth", "exifImageHeight", "fileSizeInByte",
        "fNumber", "focalLength", "exposureTime", "lensModel",
    ),
    "smart_search": ("assetId", "embedding"),
}

BINARIES = ("exiftool", "ffmpeg", "ffprobe")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def check_config(config: Config) -> list[Check]:
    if config.source is None:
        return [Check("config", WARN, "no config file found (IMMY_CONFIG or ~/.immy/config.yml)")]
    sections = [
        name for name, value in (
            ("immich", config.immich), ("pg", config.pg),
            ("media", config.media), ("ml", config.ml),
        ) if value is not None
    ]
    return [Check(
        "config", OK,
        f"{config.source} — sections: {', '.join(sections) or 'none'}",
    )]


def check_binaries(which=shutil.which) -> list[Check]:
    checks = []
    for name in BINARIES:
        path = which(name)
        checks.append(
            Check(f"bin {name}", OK, path) if path
            else Check(f"bin {name}", FAIL, "not on PATH")
        )
    try:
        import pyvips

        checks.append(Check(
            "libvips", OK,
            f"{pyvips.version(0)}.{pyvips.version(1)}.{pyvips.version(2)}",
        ))
    except Exception as exc:  # noqa: BLE001 — ImportError or a missing .so (OSError)
        checks.append(Check("libvips", FAIL, f"{type(exc).__name__}: {exc}"[:200]))
    return checks


def check_paths(config: Config) -> list[Check]:
    checks = []
    for label, path in (
        ("originals_root", config.originals_root),
        ("state_root", config.state_root),
        ("sidecars_root", config.sidecars_root),
    ):
        if path is None:
            checks.append(Check(label, SKIP, "not set"))
        elif path.is_dir():
            checks.append(Check(label, OK, str(path)))
        else:
            checks.append(Check(label, FAIL, f"{path} is not a directory"))
    if config.media is None:
        checks.append(Check("media roots", SKIP, "no media section"))
    else:
        host = Path(config.media.host_root)
        # host_root is the NAS-side path; from the Mac it is legitimately
        # absent, so its absence is a warning, not a failure.
        checks.append(
            Check("media host_root", OK, str(host)) if host.is_dir()
            else Check("media host_root", WARN, f"{host} not visible from this host")
        )
        if not config.media.container_root.startswith("/"):
            checks.append(Check(
                "media container_root", FAIL,
                f"{config.media.container_root!r} is not an absolute path",
            ))
    return checks


def check_immich(config: Config, client_factory=None) -> list[Check]:
    if config.immich is None:
        return [Check("immich api", SKIP, "no immich section")]
    from .immich import ImmichClient, ImmichError

    factory = client_factory or (lambda: ImmichClient(
        url=config.immich.url, api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    ))
    client = factory()
    try:
        about = client._request("GET", "/api/server/about") or {}
        checks = [Check("immich api", OK, f"{config.immich.url} — v{about.get('version', '?')}")]
    except ImmichError as exc:
        return [Check("immich api", FAIL, str(exc)[:200])]
    try:
        library = client._request("GET", f"/api/libraries/{config.immich.library_id}") or {}
    except ImmichError as exc:
        checks.append(Check("immich library", FAIL, str(exc)[:200]))
        return checks
    paths = library.get("importPaths") or []
    checks.append(
        Check("immich library", OK, f"{library.get('name', '?')} — {', '.join(paths)}") if paths
        else Check("immich library", FAIL, "library has no import paths")
    )
    return checks


def check_postgres(config: Config, connect=None) -> list[Check]:
    if config.pg is None:
        return [Check("postgres", SKIP, "no pg section")]
    from . import pg as pg_mod

    try:
        conn = (connect or pg_mod.connect)(config.pg)
    except Exception as exc:  # noqa: BLE001 — psycopg raises many types
        return [Check("postgres", FAIL, f"{type(exc).__name__}: {exc}"[:200])]
    try:
        checks = [Check("postgres", OK, f"{config.pg.host}:{config.pg.port}/{config.pg.database}")]
        checks += _check_columns(conn)
        checks += _check_clip_dim(conn, config)
        if config.immich is not None:
            try:
                info = pg_mod.fetch_library_info(conn, config.immich.library_id)
                checks.append(Check("library row", OK, f"import path {info.container_root}"))
            except LookupError as exc:
                checks.append(Check("library row", FAIL, str(exc)))
        return checks
    finally:
        conn.close()


def _check_columns(conn) -> list[Check]:
    checks = []
    for table, wanted in DIRECT_WRITE_COLUMNS.items():
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        ).fetchall()
        have = {r[0] for r in rows}
        if not have:
            checks.append(Check(f"table {table}", FAIL, "missing"))
            continue
        missing = [c for c in wanted if c not in have]
        checks.append(
            Check(f"table {table}", FAIL, f"missing columns: {', '.join(missing)}") if missing
            else Check(f"table {table}", OK, f"{len(wanted)} direct-write columns present")
        )
    return checks


def _check_clip_dim(conn, config: Config) -> list[Check]:
    from . import pg as pg_mod

    try:
        dim = pg_mod.fetch_smart_search_dim(conn)
    except LookupError as exc:
        return [Check("clip dim", FAIL, str(exc))]
    model = config.ml.clip_model if config.ml else None
    if model is None:
        return [Check("clip dim", SKIP, f"smart_search is vector({dim}); no ml.clip_model set")]
    expected = CLIP_DIMS.get(model)
    if expected is None:
        return [Check("clip dim", WARN, f"smart_search is vector({dim}); unknown model {model}")]
    if dim != expected:
        return [Check("clip dim", FAIL, f"smart_search is vector({dim}) but {model} emits {expected}")]
    return [Check("clip dim", OK, f"vector({dim}) matches {model}")]


def check_ml_endpoints(config: Config) -> list[Check]:
    """Only that the configured backends are *coherent* — an HTTP backend
    needs its URL. Reachability is left to the run itself (a caption server
    may legitimately be asleep until needed)."""
    if config.ml is None:
        return [Check("ml", SKIP, "no ml section")]
    checks = []
    if config.ml.clip_backend == "immich-ml" and not config.ml.immich_ml_url:
        checks.append(Check("ml clip", FAIL, "clip_backend immich-ml needs immich_ml_url"))
    else:
        checks.append(Check("ml clip", OK, config.ml.clip_backend))
    from .asr.registry import KNOWN_BACKENDS

    if config.ml.whisper_backend not in KNOWN_BACKENDS:
        checks.append(Check(
            "ml whisper", FAIL,
            f"unknown whisper_backend {config.ml.whisper_backend!r} (known: {', '.join(KNOWN_BACKENDS)})",
        ))
    elif config.ml.whisper_backend != "mlx" and not config.ml.whisper_endpoint:
        checks.append(Check(
            "ml whisper", FAIL, f"whisper_backend {config.ml.whisper_backend} needs whisper_endpoint",
        ))
    else:
        checks.append(Check("ml whisper", OK, config.ml.whisper_backend))
    return checks


def run_all(config: Config) -> list[Check]:
    checks: list[Check] = []
    for step in (
        lambda: check_config(config),
        check_binaries,
        lambda: check_paths(config),
        lambda: check_ml_endpoints(config),
        lambda: check_immich(config),
        lambda: check_postgres(config),
    ):
        try:
            checks += step()
        except Exception as exc:  # noqa: BLE001 — a doctor must never crash
            checks.append(Check("doctor", FAIL, f"check crashed: {type(exc).__name__}: {exc}"))
    return checks

