"""Local HTTP server for the ``teslacam review`` moderation viewer.

Stdlib only.  Serves the single-file viewer (``index.html`` next to this
module), a tiny JSON API over ``events.json`` / ``decisions-<dump>.json`` in
the review directory, and the media files under the dump root with HTTP
Range support (the browser scrubs 60 s segments by byte range; without 206
responses every seek would download the whole file).

Routes
    GET  /                      viewer
    GET  /api/events            <review_dir>/events.json verbatim
    GET  /api/decisions         {event id: decision} for every event (defaults filled in)
    POST /api/decisions         {"id": ..., partial decision} -> full decision
    GET  /media/<rel path>      file under root, Range aware; escapes rejected
    GET  /thumbs/<name>         file under <review_dir>/thumbs; escapes rejected

Run::

    python -m teslacam.viewer.server --root /path/to/TeslaDumps --review-dir /path/to/_review
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cull.core.http import ApiError, MediaHandler, QuietServer
from cull.core.jsonio import now_iso as _now_iso
from cull.core.jsonio import write_json_atomic as _write_json_atomic

__all__ = ["RangeHandler", "make_server", "serve", "DEFAULT_DECISION", "KEEP_STATES"]

KEEP_STATES = ("unreviewed", "keep", "drop")
DEFAULT_DECISION: dict[str, Any] = {
    "keep": "unreviewed",
    "cameras": None,
    "in_s": None,
    "out_s": None,
    "precise": False,
    "tags": [],
    "note": "",
    "archive": False,
    "lat": None,     # location set by hand (see decisions.Decision)
    "lon": None,
    "city": "",
    "updated": None,
}
_DECISION_KEYS = frozenset(DEFAULT_DECISION) - {"updated"}
_DUMP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
_WRITE_LOCK = threading.Lock()


# ----------------------------------------------------------------------------- files

def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class ReviewStore:
    """events.json (read-only) + decisions-<dump>.json (read/write) in one directory."""

    def __init__(self, review_dir: Path):
        self.review_dir = Path(review_dir)

    # events ------------------------------------------------------------------
    @property
    def events_path(self) -> Path:
        return self.review_dir / "events.json"

    def load_events(self) -> dict[str, Any]:
        try:
            data = _read_json(self.events_path)
        except FileNotFoundError:
            raise ApiError(404, f"{self.events_path} not found; run `teslacam scan` first")
        except ValueError as e:
            raise ApiError(500, f"{self.events_path}: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ApiError(500, f"{self.events_path}: unexpected shape")
        return data

    def events_by_id(self) -> dict[str, dict[str, Any]]:
        try:
            evs = self.load_events()["events"]
        except ApiError:
            return {}
        return {str(e.get("id")): e for e in evs if isinstance(e, dict) and e.get("id")}

    # decisions ---------------------------------------------------------------
    def decisions_path(self, dump: str) -> Path:
        return self.review_dir / f"decisions-{dump}.json"

    def load_dump(self, dump: str) -> dict[str, Any]:
        path = self.decisions_path(dump)
        try:
            data = _read_json(path)
        except FileNotFoundError:
            return {"schema": 1, "dump": dump, "events": {}}
        except ValueError as e:
            raise ApiError(500, f"{path}: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("events"), dict):
            raise ApiError(500, f"{path}: unexpected shape")
        data.setdefault("schema", 1)
        data.setdefault("dump", dump)
        return data

    def all_decisions(self) -> dict[str, dict[str, Any]]:
        """Merged {event id: decision}; every event in events.json gets at least the default."""
        out: dict[str, dict[str, Any]] = {}
        for eid in self.events_by_id():
            out[eid] = dict(DEFAULT_DECISION)
        try:
            files = sorted(self.review_dir.glob("decisions-*.json"))
        except OSError:
            files = []
        for path in files:
            try:
                data = _read_json(path)
            except (OSError, ValueError):
                continue
            evs = data.get("events") if isinstance(data, dict) else None
            if not isinstance(evs, dict):
                continue
            for eid, dec in evs.items():
                if isinstance(dec, dict):
                    out[str(eid)] = {**DEFAULT_DECISION, **dec}
        return out

    def update(self, req: dict[str, Any]) -> dict[str, Any]:
        """Merge a partial decision into the right dump file; returns the full decision."""
        if not isinstance(req, dict):
            raise ApiError(400, "body must be a JSON object")
        eid = req.get("id")
        if not isinstance(eid, str) or not eid:
            raise ApiError(400, "missing id")
        unknown = set(req) - _DECISION_KEYS - {"id"}
        if unknown:
            raise ApiError(400, f"unknown field(s): {', '.join(sorted(unknown))}")
        dump = eid.split("|")[0]
        if not _DUMP_RE.match(dump) or ".." in dump:
            raise ApiError(400, f"bad event id {eid!r}")
        event = self.events_by_id().get(eid)
        patch = _validate_patch(req, event)
        with _WRITE_LOCK:
            data = self.load_dump(dump)
            cur = {**DEFAULT_DECISION, **data["events"].get(eid, {})}
            cur.update(patch)
            _validate_window(cur, event)
            cur["updated"] = _now_iso()
            data["events"][eid] = cur
            _write_json_atomic(self.decisions_path(dump), data)
        return cur


def _validate_patch(req: dict[str, Any], event: dict[str, Any] | None) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for key, val in req.items():
        if key == "id":
            continue
        if key == "keep":
            if val not in KEEP_STATES:
                raise ApiError(400, f"keep must be one of {', '.join(KEEP_STATES)}")
            patch[key] = val
        elif key == "cameras":
            if val is None:
                patch[key] = None
            elif isinstance(val, list) and all(isinstance(c, str) for c in val):
                cams = list(dict.fromkeys(val))
                if event is not None:
                    present = event.get("cams") or []
                    bad = [c for c in cams if c not in present]
                    if bad:
                        raise ApiError(400, f"camera(s) not present on this event: {', '.join(bad)}")
                    cams = [c for c in present if c in cams]
                patch[key] = cams
            else:
                raise ApiError(400, "cameras must be null or a list of camera names")
        elif key in ("in_s", "out_s"):
            if val is None:
                patch[key] = None
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                v = round(float(val), 2)
                if v < 0:
                    raise ApiError(400, f"{key} must be >= 0")
                patch[key] = v
            else:
                raise ApiError(400, f"{key} must be null or a number")
        elif key in ("precise", "archive"):
            if not isinstance(val, bool):
                raise ApiError(400, f"{key} must be a boolean")
            patch[key] = val
        elif key == "tags":
            if not isinstance(val, list) or not all(isinstance(t, str) for t in val):
                raise ApiError(400, "tags must be a list of strings")
            patch[key] = list(dict.fromkeys(t.strip().lstrip("#").lower() for t in val if t.strip().lstrip("#")))
        elif key == "note":
            if not isinstance(val, str):
                raise ApiError(400, "note must be a string")
            patch[key] = val.strip()
        elif key in ("lat", "lon"):
            # a location set by hand (decisions.Decision.lat/lon/city)
            if val is None:
                patch[key] = None
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                limit = 90.0 if key == "lat" else 180.0
                if not -limit <= float(val) <= limit:
                    raise ApiError(400, f"{key} out of range")
                patch[key] = round(float(val), 6)
            else:
                raise ApiError(400, f"{key} must be null or a number")
        elif key == "city":
            if not isinstance(val, str):
                raise ApiError(400, "city must be a string")
            patch[key] = val.strip()
    if ("lat" in patch) != ("lon" in patch):
        raise ApiError(400, "lat and lon come together")
    if "lat" in patch and patch["lat"] is None:
        patch["city"] = ""
    return patch


def _validate_window(dec: dict[str, Any], event: dict[str, Any] | None) -> None:
    a, b = dec.get("in_s"), dec.get("out_s")
    if event is not None:
        total = event.get("total")
        if isinstance(total, (int, float)):
            for key, v in (("in_s", a), ("out_s", b)):
                if v is not None and v > total + 0.01:
                    raise ApiError(400, f"{key} beyond the event end ({total:.1f} s)")
    if a is not None and b is not None and a >= b:
        raise ApiError(400, "in_s must be before out_s")


# ----------------------------------------------------------------------------- handler

class RangeHandler(MediaHandler):
    """Viewer + JSON API + Range-aware media handler.

    Construct through ``functools.partial(RangeHandler, root=..., review_dir=...)``
    (see :func:`make_server`).
    """

    server_version = "teslacam-viewer/1"
    viewer_dir = Path(__file__).resolve().parent

    def __init__(self, *args, root: Path, review_dir: Path, **kwargs):
        self.root = Path(root).resolve()
        self.store = ReviewStore(Path(review_dir))
        super().__init__(*args, **kwargs)

    # helpers ------------------------------------------------------------------
    def _resolve_media(self, rel: str) -> Path | None:
        """Path under root for a /media/<rel> URL."""
        return self._resolve_under(self.root, rel)

    def _resolve_thumb(self, rel: str) -> Path | None:
        """Path under <review_dir>/thumbs for a /thumbs/<name> URL."""
        return self._resolve_under(self.store.review_dir / "thumbs", rel)

    # routes -------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self._route_get()
        except ApiError as e:
            self._send_error_json(e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser dropped a media request mid-stream; normal when scrubbing

    def _route_get(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self._send_file(self.viewer_dir / "index.html", cache="no-cache")
        if path == "/api/events":
            return self._send_json(200, self.store.load_events())
        if path == "/api/decisions":
            return self._send_json(200, self.store.all_decisions())
        if path.startswith("/media/"):
            full = self._resolve_media(path[len("/media/"):])
            if full is None:
                return self._send_error_json(404, "not found")
            return self._send_file(full)
        if path.startswith("/thumbs/"):
            full = self._resolve_thumb(path[len("/thumbs/"):])
            if full is None:
                return self._send_error_json(404, "not found")
            return self._send_file(full, cache="max-age=3600")
        # static siblings of index.html (only if the viewer is ever split into files)
        name = path.lstrip("/")
        if name and "/" not in name and not name.startswith(".") and name != "server.py" \
                and not name.endswith(".py") and (self.viewer_dir / name).is_file():
            return self._send_file(self.viewer_dir / name, cache="no-cache")
        self._send_error_json(404, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path != "/api/decisions":
                raise ApiError(404, "not found")
            self._send_json(200, self.store.update(self._read_json_body()))
        except ApiError as e:
            self._send_error_json(e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ----------------------------------------------------------------------------- entry points

def make_server(root: Path, review_dir: Path, port: int = 8777, host: str = "127.0.0.1") -> QuietServer:
    """Bound but not yet serving; ``port=0`` picks an ephemeral port (tests)."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"root is not a directory: {root}")
    handler = functools.partial(RangeHandler, root=root, review_dir=Path(review_dir))
    return QuietServer((host, port), handler)


def serve(root: Path, review_dir: Path, port: int = 8777, open_browser: bool = True,
          host: str = "127.0.0.1") -> None:
    """Serve until Ctrl-C.  Prints one line with the URL."""
    try:
        srv = make_server(root, review_dir, port, host)
    except OSError as e:
        print(f"Could not bind {host}:{port}: {e}", file=sys.stderr)
        print(f"Is the viewer already running?  Try http://{host}:{port}/", file=sys.stderr)
        raise SystemExit(1)
    url = f"http://{srv.server_address[0]}:{srv.server_address[1]}/"
    print(f"teslacam review  {url}  (root {Path(root).resolve()}, review {Path(review_dir).resolve()})")
    print("Ctrl-C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m teslacam.viewer.server", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, type=Path, help="dump root (parent of TeslaDump*/ folders)")
    ap.add_argument("--review-dir", required=True, type=Path, help="directory holding events.json and decisions-*.json")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    serve(a.root, a.review_dir, port=a.port, open_browser=not a.no_browser)


if __name__ == "__main__":
    main()
