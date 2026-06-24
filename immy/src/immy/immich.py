"""Thin Immich REST client — only the endpoints `immy promote` calls.

Uses `urllib` to avoid a new dependency. Short timeouts because the Mac is
often on a 5–10 Mbps uplink; a hung API call should fail fast and let the
caller retry, not block for 60 s on connect.

Endpoints used:

- `POST /api/libraries/{id}/scan`   — kick off an external-library scan.
- `POST /api/search/metadata`       — find an asset by original filename.
- `POST /api/stacks`                — collapse `.insv` + `.lrv` into one tile.
- `GET  /api/albums`                — list albums (used to find-by-name).
- `POST /api/albums`                — create an album with name/description.
- `PATCH /api/albums/{id}`          — update name/description.
- `PUT  /api/albums/{id}/assets`    — add assets to an existing album.

The Immich API surface shifts between minor versions; keep the touched
surface small and version-pin when it matters.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_TIMEOUT = 10.0  # seconds — caller may raise for bigger ops

# Hosts on the Tailscale tailnet are not reachable through any normal
# HTTP(S) proxy — the proxy returns "503 CONNECT tunnel failed" on the
# private hostname. urllib otherwise honours HTTP_PROXY/HTTPS_PROXY env
# vars set by sysadmins / the parent process (Claude Code, work VPN
# helpers, etc.), so we build our own opener with an empty ProxyHandler
# whenever the target host looks like a tailnet name. The user shouldn't
# have to remember to export NO_PROXY before every `immy` invocation.
_TAILNET_HOST_SUFFIXES = (".ts.net",)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_tailnet_host(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host.endswith(s) for s in _TAILNET_HOST_SUFFIXES)


class ImmichError(RuntimeError):
    """Any non-2xx response or transport failure."""


@dataclass
class ImmichClient:
    url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT
    # When set, every request runs `curl` ON this host over ssh instead of
    # opening a socket locally (see ImmichConfig.ssh_host). `url` is then the
    # address as seen FROM ssh_host (e.g. http://127.0.0.1:2283).
    ssh_host: str | None = None

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        if self.ssh_host:
            return self._request_ssh(method, path, body)
        url = f"{self.url.rstrip('/')}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        _open = (
            _NO_PROXY_OPENER.open if _is_tailnet_host(url)
            else urllib.request.urlopen
        )
        try:
            with _open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace") if e.fp else ""
            raise ImmichError(f"{method} {path} → {e.code}: {detail[:400]}") from e
        except urllib.error.URLError as e:
            raise ImmichError(f"{method} {path} → transport: {e.reason}") from e

    def _request_ssh(self, method: str, path: str, body: dict | None = None) -> Any:
        """Same contract as `_request`, but the HTTP call is made by `curl`
        running on `self.ssh_host` (`ssh <host> curl …`). Used for n5, whose
        Immich API is localhost-bound and not reachable from the laptop (no
        port-forwarding, TLS handshake fails on the tailscale IP).

        The request body is piped to curl over stdin (`--data-binary @-`) so JSON
        never has to survive remote-shell quoting; the remote argv is otherwise
        `shlex.quote`d into one command string (ssh joins args with bare spaces,
        so pre-quoting is mandatory). A trailing `-w '\\n%{http_code}'` carries
        the status out-of-band — curl exits 0 on HTTP 4xx/5xx (no `-f`), so we
        read the code and mirror `_request`'s error/None/JSON behaviour."""
        url = f"{self.url.rstrip('/')}{path}"
        data = json.dumps(body).encode() if body is not None else None
        curl = [
            "curl", "-sS", "-X", method,
            "-H", f"x-api-key: {self.api_key}",
            "-H", "Accept: application/json",
            "-w", "\n%{http_code}",
        ]
        if data is not None:
            curl += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        curl.append(url)
        remote = " ".join(shlex.quote(a) for a in curl)
        ssh = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            self.ssh_host, remote,
        ]
        try:
            proc = subprocess.run(
                ssh, input=data, capture_output=True,
                timeout=self.timeout + 15,  # ssh connect/auth overhead
            )
        except subprocess.TimeoutExpired as e:
            raise ImmichError(f"{method} {path} → transport: ssh timeout") from e
        except OSError as e:  # ssh binary missing, etc.
            raise ImmichError(f"{method} {path} → transport: {e}") from e
        if proc.returncode != 0:
            # ssh failure (255) or curl connect failure — not an HTTP status.
            err = proc.stderr.decode(errors="replace").strip()
            raise ImmichError(
                f"{method} {path} → transport: ssh/curl rc={proc.returncode}: {err[:400]}"
            )
        out = proc.stdout.decode(errors="replace")
        raw, _, code_s = out.rpartition("\n")
        try:
            code = int(code_s.strip())
        except ValueError:
            raise ImmichError(
                f"{method} {path} → transport: no status from curl: {out[:200]}"
            )
        if not 200 <= code < 300:
            raise ImmichError(f"{method} {path} → {code}: {raw[:400]}")
        raw = raw.strip()
        if not raw:
            return None
        return json.loads(raw)

    def scan_library(self, library_id: str) -> None:
        """Fire-and-forget. Immich returns 204 and scans async in the background."""
        self._request("POST", f"/api/libraries/{library_id}/scan", body={})

    def get_jobs(self) -> dict[str, Any]:
        """`GET /api/jobs` → per-queue stats. Read before triggering a re-embed
        so the summary can show what was pending: each value has `jobCounts`
        (active/waiting/failed/…) and `queueStatus` (isActive/isPaused)."""
        return self._request("GET", "/api/jobs") or {}

    def queue_job(self, name: str, *, force: bool = False) -> None:
        """Start a library-wide job. `PUT /api/jobs/{name}` body
        `{command:"start", force}` — the same control the admin Jobs page uses
        (the legacy run-queue route, still live in v2.7.x; returns 200).

        `name` is a queue id, e.g. `smartSearch` (CLIP embeddings) or
        `faceDetection`. `force=False` processes only assets MISSING that output
        (the safe per-promote default — embeds the freshly-inserted assets that
        Immich's library scan won't auto-queue because immy pre-inserted their
        rows). `force=True` reprocesses EVERY asset — the one-time index cleanup
        when the stored vectors are a heterogeneous mix of model eras.

        NB: this is LIBRARY-WIDE, not per-trip — each call rescans the whole
        library for work. In a batch promote, run it ONCE at the end, not per
        trip (especially `force=True`)."""
        self._request(
            "PUT", f"/api/jobs/{name}",
            body={"command": "start", "force": force},
        )

    def regenerate_thumbnails(self, asset_ids: list[str]) -> None:
        """Queue thumbnail (re)generation for specific assets.

        `POST /api/assets/jobs` with name `regenerate-thumbnail`. Used by
        promote to repair assets that were registered while their originals
        were still offline (Immich wrote a `__offline_placeholder__` thumb,
        or none) and never re-thumbnailed after the files landed on the NAS —
        clearing `isOffline` alone does NOT re-queue derivative jobs. Batched
        because Immich caps the request body; fire-and-forget (204)."""
        if not asset_ids:
            return
        for i in range(0, len(asset_ids), 1000):
            self._request(
                "POST", "/api/assets/jobs",
                body={"assetIds": asset_ids[i:i + 1000], "name": "regenerate-thumbnail"},
            )

    def refresh_metadata(self, asset_ids: list[str]) -> None:
        """Queue a metadata re-extraction for specific assets.

        `POST /api/assets/jobs` with name `refresh-metadata` — the same job
        Immich's "Refresh metadata" admin action runs. Used by the
        `srt verify-channel` probe to prove which GPS write survives a
        refresh (Immich re-reads file/container tags and overwrites every
        UNLOCKED `asset_exif` field). Batched + fire-and-forget (204)."""
        if not asset_ids:
            return
        for i in range(0, len(asset_ids), 1000):
            self._request(
                "POST", "/api/assets/jobs",
                body={"assetIds": asset_ids[i:i + 1000], "name": "refresh-metadata"},
            )

    def find_asset_id(
        self,
        original_file_name: str,
        *,
        original_path_suffix: str | None = None,
    ) -> str | None:
        """Return the first asset whose `originalFileName` matches, or None.

        When `original_path_suffix` is given (e.g. `"/2026-04-foo/IMG_1.jpg"`),
        filter results to assets whose `originalPath` ends with that suffix.
        Needed when the same filename exists under multiple trip folders —
        plain filename search returns an arbitrary collision and the album
        gets assets from the wrong trip.
        """
        resp = self._request(
            "POST",
            "/api/search/metadata",
            body={"originalFileName": original_file_name},
        )
        items = (resp or {}).get("assets", {}).get("items", []) or []
        if original_path_suffix is not None:
            items = [a for a in items if a.get("originalPath", "").endswith(original_path_suffix)]
        return items[0]["id"] if items else None

    def create_stack(self, primary_asset_id: str, other_asset_ids: list[str]) -> str | None:
        """Immich 2.x: primary is first in `assetIds`; there's no separate `primaryAssetId`."""
        payload = {"assetIds": [primary_asset_id, *other_asset_ids]}
        resp = self._request("POST", "/api/stacks", body=payload)
        return (resp or {}).get("id") if isinstance(resp, dict) else None

    # --- albums ------------------------------------------------------------

    def find_album_by_name(self, name: str) -> dict | None:
        """Return the first album matching `name` exactly, or None.

        Immich's `GET /api/albums` returns all albums owned by the caller;
        for personal use the list is small enough that client-side filter
        is fine. If multiple albums share a name we pick the first — the
        promote flow is creating/updating, so the deterministic choice
        beats a ranked heuristic.
        """
        resp = self._request("GET", "/api/albums")
        if not isinstance(resp, list):
            return None
        for album in resp:
            if isinstance(album, dict) and album.get("albumName") == name:
                return album
        return None

    def create_album(
        self,
        name: str,
        *,
        description: str | None = None,
        asset_ids: list[str] | None = None,
    ) -> str | None:
        body: dict = {"albumName": name}
        if description is not None:
            body["description"] = description
        if asset_ids:
            body["assetIds"] = asset_ids
        resp = self._request("POST", "/api/albums", body=body)
        return (resp or {}).get("id") if isinstance(resp, dict) else None

    def update_album(
        self,
        album_id: str,
        *,
        description: str | None = None,
    ) -> None:
        """Patch album fields. Today: description only (album name is the
        identity key — renaming isn't part of the promote flow)."""
        body: dict = {}
        if description is not None:
            body["description"] = description
        if not body:
            return
        self._request("PATCH", f"/api/albums/{album_id}", body=body)

    def add_assets_to_album(
        self, album_id: str, asset_ids: list[str]
    ) -> list[dict]:
        """`PUT /api/albums/{id}/assets` is idempotent: already-member assets
        come back with `success=false, error='duplicate'`, never fails hard.

        Returns the per-asset result list (may be empty on no-op)."""
        if not asset_ids:
            return []
        resp = self._request(
            "PUT", f"/api/albums/{album_id}/assets", body={"ids": asset_ids}
        )
        return resp if isinstance(resp, list) else []


def wait_for_asset(
    client: ImmichClient,
    original_file_name: str,
    *,
    tries: int = 6,
    delay: float = 2.0,
    original_path_suffix: str | None = None,
) -> str | None:
    """Poll search-by-filename until the newly-scanned asset appears. Returns
    the asset id or None if it never shows up within the budget.

    `immy promote` triggers a library scan, then looks up IDs for files it
    wants to stack. Scans are async so we give Immich a few seconds to index.

    Pass `original_path_suffix` to disambiguate when multiple trip folders
    carry the same filename (see `find_asset_id`).
    """
    for _ in range(tries):
        aid = client.find_asset_id(
            original_file_name, original_path_suffix=original_path_suffix,
        )
        if aid:
            return aid
        time.sleep(delay)
    return None
