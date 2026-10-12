"""Stdlib HTTP plumbing shared by the cull and teslacam viewers.

Range-aware file responses (browsers scrub video by byte range; without 206
every seek downloads the whole file), path resolution that refuses escapes,
JSON replies, a POST guard against cross-site writes, and a threading server
whose listen queue survives a burst of <video> requests.
"""

from __future__ import annotations

import http.server
import json
import mimetypes
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

mimetypes.add_type("video/mp4", ".lrf")  # DJI proxy: an MP4 under another name
mimetypes.add_type("text/plain", ".srt")

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Slice:
    """File wrapper that stops after n bytes, so copyfileobj() sends only the range."""

    def __init__(self, f, n: int):
        self.f, self.left = f, n

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        if size < 0 or size > self.left:
            size = self.left
        data = self.f.read(size)
        self.left -= len(data)
        return data

    def close(self) -> None:
        self.f.close()


def resolve_under(base: Path, rel: str) -> Path | None:
    """File under `base` for a URL-relative path, or None if it escapes or is not a file."""
    rel = unquote(rel)
    if "\x00" in rel or rel.startswith("/"):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    try:
        base = Path(base).resolve()
        full = base.joinpath(*parts).resolve()
    except (OSError, ValueError):
        return None
    if base not in full.parents or not full.is_file():
        return None
    return full


class MediaHandler(http.server.BaseHTTPRequestHandler):
    """Base handler: quiet log, JSON replies, Range-aware files, guarded JSON bodies."""

    protocol_version = "HTTP/1.1"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by base class
        pass

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(self, code: int, message: str) -> None:
        self._send_json(code, {"error": message})

    def _resolve_under(self, base: Path, rel: str) -> Path | None:
        return resolve_under(base, rel)

    def _read_json_body(self) -> Any:
        """The POST body as JSON. A cross-site form cannot send application/json
        without a preflight this server never answers, and a browser always
        sends Origin on a cross-origin POST — so both are checked here."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        origin = self.headers.get("Origin")
        if origin is not None and urlsplit(origin).netloc != (self.headers.get("Host") or ""):
            raise ApiError(403, "cross-origin request refused")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad Content-Length")
        if n <= 0 or n > MAX_BODY:
            raise ApiError(400, "empty or oversized body")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ApiError(400, f"bad JSON: {e}")

    def _send_file(self, path: Path, cache: str | None = None) -> None:
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            return self._send_error_json(404, "not found")
        rng = self.headers.get("Range")
        m = RANGE_RE.match(rng.strip()) if rng else None
        start, end = 0, size - 1
        partial = False
        if m and size > 0:
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            else:
                m = None
            end = min(end, size - 1)
            if m and (start >= size or start > end):
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = m is not None
        length = end - start + 1 if size else 0
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command == "HEAD":
            f.close()
            return
        try:
            f.seek(start)
            shutil.copyfileobj(Slice(f, length), self.wfile, 256 * 1024)
        finally:
            f.close()


class QuietServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Python < 3.13 listens with a backlog of 5; macOS RESETS connections beyond it
    # instead of queueing them. Six <video> tags plus thumbnails open more than that in
    # one burst, and a reset media request leaves the player stalled.
    request_queue_size = 128

    def handle_error(self, request, client_address):  # keep the terminal quiet on dropped sockets
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)
