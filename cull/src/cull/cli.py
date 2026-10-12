"""`cull`: scan import candidates → review (keep/drop, trip) → stage into trip folders."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
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
        console.print(f"[yellow]Immich DB not available ({e}); no Immich checks[/yellow]")
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
    doc = scan_mod.scan(work, src, immich=im, geocode=geocode, proxies=proxies, log=console.print)
    t = Table(title=f"{len(doc['items'])} items → {work / '_cull' / 'items.json'}")
    for col in ("source", "videos", "photos", "GB", "in Immich?", "flagged", "orphans"):
        t.add_column(col, justify="left" if col == "source" else "right")
    for name in doc["sources"]:
        its = [i for i in doc["items"] if i["src"] == name]
        t.add_row(name, str(sum(i["kind"] == "video" for i in its)), str(sum(i["kind"] == "photo" for i in its)),
                  f"{sum(i['size'] for i in its) / 1e9:.1f}", str(sum(bool(i["immich"]) for i in its)),
                  str(sum(bool(i["flags"]) for i in its)), str(sum(o["src"] == name for o in doc["orphans"])))
    console.print(t)
