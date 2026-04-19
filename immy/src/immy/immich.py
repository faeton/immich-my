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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_TIMEOUT = 10.0  # seconds — caller may raise for bigger ops


class ImmichError(RuntimeError):
    """Any non-2xx response or transport failure."""


@dataclass
class ImmichClient:
    url: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.url.rstrip('/')}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace") if e.fp else ""
            raise ImmichError(f"{method} {path} → {e.code}: {detail[:400]}") from e
        except urllib.error.URLError as e:
            raise ImmichError(f"{method} {path} → transport: {e.reason}") from e

    def scan_library(self, library_id: str) -> None:
        """Fire-and-forget. Immich returns 204 and scans async in the background."""
        self._request("POST", f"/api/libraries/{library_id}/scan", body={})

    def find_asset_id(self, original_file_name: str) -> str | None:
        """Return the first asset whose `originalFileName` matches, or None."""
        resp = self._request(
            "POST",
            "/api/search/metadata",
            body={"originalFileName": original_file_name},
        )
        items = (resp or {}).get("assets", {}).get("items", []) or []
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
) -> str | None:
    """Poll search-by-filename until the newly-scanned asset appears. Returns
    the asset id or None if it never shows up within the budget.

    `immy promote` triggers a library scan, then looks up IDs for files it
    wants to stack. Scans are async so we give Immich a few seconds to index.
    """
    for _ in range(tries):
        aid = client.find_asset_id(original_file_name)
        if aid:
            return aid
        time.sleep(delay)
    return None
