"""Local HTTP server for `cull review`.

Routes
    GET  /                          viewer
    GET  /api/items                 <work>/_cull/items.json
    GET  /api/decisions             {item id: decision} for every item (defaults filled in)
    GET  /api/trips                 existing trip folder names under --trips
    POST /api/decisions             {"ids": [...], "patch": {...}} -> {"decisions": {...}}
    GET  /media/<src>/<rel>         file under that source, Range aware; escapes rejected
    GET  /work/<sub>/<name>         thumbs/, proxies/, previews/ under <work>/_cull
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .. import decisions as dec
from ..core.http import ApiError, MediaHandler, QuietServer
from ..scan import cull_dir, load_items

WORK_SUBDIRS = ("thumbs", "proxies", "previews")


class CullHandler(MediaHandler):
    server_version = "cull/1"
    viewer_dir = Path(__file__).resolve().parent

    def __init__(self, *args, work: Path, trips: Path | None, **kwargs):
        self.work = Path(work)
        self.trips = Path(trips) if trips else None
        super().__init__(*args, **kwargs)

    def _doc(self) -> dict:
        try:
            return load_items(self.work)
        except FileNotFoundError as e:
            raise ApiError(404, str(e))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self._route_get()
        except ApiError as e:
            self._send_error_json(e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route_get(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(self.viewer_dir / "index.html", cache="no-cache")
        if path == "/api/items":
            return self._send_json(200, self._doc())
        if path == "/api/decisions":
            doc = self._doc()
            decs = dec.load(self.work)
            return self._send_json(200, {i["id"]: dec.effective(i, decs.get(i["id"])) for i in doc["items"]})
        if path == "/api/trips":
            return self._send_json(200, dec.list_trips(self.trips))
        if path.startswith("/media/"):
            src, _, rel = path[len("/media/"):].partition("/")
            root = self._doc()["sources"].get(unquote(src))
            full = self._resolve_under(Path(root), rel) if root else None
            if full is None:
                return self._send_error_json(404, "not found")
            return self._send_file(full)
        if path.startswith("/work/"):
            sub, _, rel = path[len("/work/"):].partition("/")
            full = self._resolve_under(cull_dir(self.work) / sub, rel) if sub in WORK_SUBDIRS else None
            if full is None:
                return self._send_error_json(404, "not found")
            return self._send_file(full, cache="max-age=3600")
        self._send_error_json(404, "not found")

    def do_POST(self) -> None:
        try:
            if urlsplit(self.path).path != "/api/decisions":
                raise ApiError(404, "not found")
            req = self._read_json_body()
            if not isinstance(req, dict):
                raise ApiError(400, "body must be an object")
            items = {i["id"]: i for i in self._doc()["items"]}
            try:
                out = dec.apply(self.work, items, req.get("ids"), req.get("patch"), set(dec.list_trips(self.trips)))
            except ValueError as e:
                raise ApiError(400, str(e))
            self._send_json(200, {"decisions": out})
        except ApiError as e:
            self._send_error_json(e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(work: Path, trips: Path | None, port: int = 8778, host: str = "127.0.0.1") -> QuietServer:
    """Bound but not yet serving; `port=0` picks an ephemeral port (tests)."""
    handler = functools.partial(CullHandler, work=Path(work), trips=trips)
    return QuietServer((host, port), handler)


def serve(work: Path, trips: Path | None, port: int = 8778, host: str = "127.0.0.1") -> None:
    try:
        srv = make_server(work, trips, port, host)
    except OSError as e:
        print(f"Could not bind {host}:{port}: {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"cull review  http://{srv.server_address[0]}:{srv.server_address[1]}/  (work {work})")
    print("Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
