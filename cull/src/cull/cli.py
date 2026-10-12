"""`cull`: scan import candidates → review (keep/drop, trip) → stage into trip folders."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import scan as scan_mod

app = typer.Typer(help=__doc__, no_args_is_help=True)
console = Console()

WORK_OPT = typer.Option(..., "--work", "-w", help="Working dir; state goes to <work>/_cull.")
IMMICH_CFG_OPT = typer.Option(Path(os.environ.get("CULL_IMMICH_CONFIG", "/mnt/flash/immy/config.host.yml")),
                              "--immich-config", help="immy config with the `pg:` block (read-only use).")


def _immich(config: Path):
    try:
        from .immich import Immich
        return Immich.connect(config)
    except Exception as e:  # missing extra, unreachable DB, bad config: a warning, not a failure
        console.print(f"[yellow]Immich DB not available ({escape(str(e))}); no Immich checks[/yellow]")
        return None


@app.callback()
def main() -> None:
    """cull"""


@app.command()
def scan(work: Path = WORK_OPT,
         src: list[Path] = typer.Option([], "--src", "-s", help="Source dir (repeatable; remembered)."),
         immich: bool = typer.Option(False, "--immich", help="Mark items that are possibly in Immich."),
         immich_config: Path = IMMICH_CFG_OPT,
         geocode: bool = typer.Option(True, "--geocode/--no-geocode"),
         proxies: bool = typer.Option(True, "--proxies/--no-proxies")):
    """Index the sources into <work>/_cull/items.json (sources are only read)."""
    work = work.expanduser().resolve()
    im = _immich(immich_config) if immich else None
    def log(msg: str) -> None:
        console.print(f"[yellow]{escape(msg)}[/yellow]" if msg.startswith("warning:") else escape(msg))

    try:
        doc = scan_mod.scan(work, src, immich=im, geocode=geocode, proxies=proxies, log=log)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    t = Table(title=escape(f"{len(doc['items'])} items → {work / '_cull' / 'items.json'}"))
    for col in ("source", "videos", "photos", "GB", "in Immich?", "flagged", "orphans"):
        t.add_column(col, justify="left" if col == "source" else "right")
    for name in doc["sources"]:
        its = [i for i in doc["items"] if i["src"] == name]
        t.add_row(escape(name), str(sum(i["kind"] == "video" for i in its)), str(sum(i["kind"] == "photo" for i in its)),
                  f"{sum(i['size'] for i in its) / 1e9:.1f}", str(sum(bool(i["immich"]) for i in its)),
                  str(sum(bool(i["flags"]) for i in its)), str(sum(o["src"] == name for o in doc["orphans"])))
    console.print(t)


@app.command()
def review(work: Path = WORK_OPT,
           trips: Path | None = typer.Option(None, "--trips", help="Trips root, for trip-name suggestions."),
           host: str = typer.Option("127.0.0.1", "--host", help="127.0.0.1, tailscale, or one address."),
           port: int = typer.Option(8778, "--port")):
    """Serve the review viewer (keep/drop, trip, tags, note)."""
    from .core.host import HostError, resolve_host
    from .viewer import server
    work = work.expanduser().resolve()
    if not (work / "_cull" / "items.json").exists():
        raise typer.BadParameter(f"{work / '_cull' / 'items.json'} missing — run `cull scan` first")
    try:
        bind = resolve_host(host)
    except HostError as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2)
    server.serve(work, trips.expanduser().resolve() if trips else None, port=port, host=bind)
