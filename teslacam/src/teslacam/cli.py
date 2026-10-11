"""`teslacam` CLI: scan → review → render."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import decisions as dec_mod
from . import render as render_mod
from . import scan as scan_mod

app = typer.Typer(help=__doc__, no_args_is_help=True)
console = Console()

ROOT_OPT = typer.Option(..., "--root", "-r", help="Dumps root (e.g. ~/Media/inbox/TeslaDumps).")
REVIEW_OPT = typer.Option(None, "--review-dir", help="Review state dir (default <root>/_review).")


def _review_dir(root: Path, review_dir: Path | None) -> Path:
    return (review_dir or root / "_review").expanduser().resolve()


@app.command()
def scan(root: Path = ROOT_OPT, review_dir: Path | None = REVIEW_OPT,
         exclude: list[str] = typer.Option(list(scan_mod.DEFAULT_EXCLUDE), "--exclude",
                                           help="Top-level dump folders to skip."),
         thumbs: bool = typer.Option(True, "--thumbs/--no-thumbs"),
         geocode: bool = typer.Option(True, "--geocode/--no-geocode",
                                      help="Fill missing cities from Nominatim (cached in <review>/geocache.json).")):
    """Index every dump under ROOT into <review>/events.json (read-only on the dumps)."""
    root = root.expanduser().resolve()
    rd = _review_dir(root, review_dir)
    doc = scan_mod.scan(root, rd, exclude=tuple(exclude), thumbs=thumbs, geocode=geocode, log=console.print)
    evs = doc["events"]
    t = Table(title=f"{len(evs)} events → {rd / 'events.json'}")
    for col in ("dump", "saved", "sentry", "drives", "hours", "gps", "tz"):
        t.add_column(col, justify="right" if col not in ("dump",) else "left")
    by: dict[str, list[dict]] = {}
    for e in evs:
        by.setdefault(e["dump"], []).append(e)
    for dump, es in sorted(by.items()):
        t.add_row(dump, str(sum(e["kind"] == "saved" for e in es)), str(sum(e["kind"] == "sentry" for e in es)),
                  str(sum(e["kind"] == "recent" for e in es)), f"{sum(e['total'] for e in es) / 3600:.1f}",
                  str(sum(e["lat"] is not None for e in es)), str(sum(e["offset"] is not None for e in es)))
    console.print(t)
    if doc["duplicates"]:
        console.print(f"[dim]{len(doc['duplicates'])} duplicate event folders skipped (see events.json → duplicates)[/dim]")


@app.command()
def review(root: Path = ROOT_OPT, review_dir: Path | None = REVIEW_OPT,
           port: int = typer.Option(8777, "--port"),
           open_browser: bool = typer.Option(True, "--open/--no-open")):
    """Serve the moderation viewer (keep/drop, cameras, in/out, tags) on localhost."""
    from .viewer import server
    root = root.expanduser().resolve()
    rd = _review_dir(root, review_dir)
    if not (rd / "events.json").exists():
        raise typer.BadParameter(f"{rd / 'events.json'} missing — run `teslacam scan` first")
    server.serve(root, rd, port=port, open_browser=open_browser)


@app.command()
def seed(root: Path = ROOT_OPT, review_dir: Path | None = REVIEW_OPT,
         tags_json: Path = typer.Option(..., "--tags-json", help="Old viewer's tags.json"),
         overwrite: bool = typer.Option(False, "--overwrite")):
    """One-time import of the old viewer's tags/notes/in-out into decisions."""
    rd = _review_dir(root.expanduser().resolve(), review_dir)
    n = dec_mod.seed_from_tags_json(rd, tags_json.expanduser(), overwrite=overwrite)
    console.print(f"seeded {n} events")


def _fmt_gb(b: int) -> str:
    return f"{b / 1e9:.1f} GB"


@app.command()
def render(root: Path = ROOT_OPT, review_dir: Path | None = REVIEW_OPT,
           out: Path = typer.Option(..., "--out", "-o", help="Render root; year folders are created under it."),
           event: list[str] = typer.Option([], "--event", "-e", help="Render only these event ids (even if not Keep)."),
           dump: list[str] = typer.Option([], "--dump", help="Only these dumps."),
           model: list[str] = typer.Option([], "--model",
                                           help="Car model: 'Model X' for every dump, or 'DUMP=Model X' per dump "
                                                "(repeatable). Unknown stays plain 'Tesla'."),
           album: str = typer.Option("Tesla Dashcam", "--album"),
           yes: bool = typer.Option(False, "--yes", "-y", help="Skip the per-dump confirmation."),
           dry_run: bool = typer.Option(False, "--dry-run"),
           force: bool = typer.Option(False, "--force", help="Re-render even when the receipt matches."),
           prune: bool = typer.Option(False, "--prune", help="Delete rendered folders of events no longer Keep (asks)."),
           open_result: bool = typer.Option(False, "--open", help="Open the mosaic(s) after rendering.")):
    """Render kept events (mosaic + ticked cameras + track + manifest) into OUT."""
    root = root.expanduser().resolve()
    rd = _review_dir(root, review_dir)
    out = out.expanduser().resolve()
    _, events = scan_mod.load_events(rd)
    decisions = dec_mod.load_all(rd)
    jobs = render_mod.plan(events, decisions, out, only_ids=set(event) or None, dumps=set(dump) or None)
    if not jobs:
        console.print("nothing to render (no events marked Keep)")
        raise typer.Exit(0)
    model_default, model_by_dump = _parse_models(model)
    model_for = lambda j: model_by_dump.get(j.ev.dump, model_default)  # noqa: E731
    todo = [j for j in jobs if force or not render_mod.is_done(j, model_for(j))]
    by: dict[str, list[render_mod.Job]] = {}
    for j in todo:
        by.setdefault(j.ev.dump, []).append(j)
    t = Table(title=f"{len(jobs)} kept · {len(todo)} to render · {len(jobs) - len(todo)} up to date")
    for col in ("dump", "events", "mosaic min", "est size", "est time"):
        t.add_column(col, justify="left" if col == "dump" else "right")
    for d, js in sorted(by.items()):
        t.add_row(d, str(len(js)), f"{sum(j.b - j.a for j in js) / 60:.0f}",
                  _fmt_gb(sum(j.est_bytes() for j in js)), f"{sum(j.est_seconds() for j in js) / 60:.0f} min")
    console.print(t)
    if dry_run:
        for j in todo:
            console.print(f"  {j.ev.id}  [{j.a:.0f}–{j.b:.0f}s] cams={','.join(j.cams)} → {j.out_dir}")
        raise typer.Exit(0)
    accepted: list[render_mod.Job] = []
    for d, js in sorted(by.items()):
        if yes or typer.confirm(f"render {d}: {len(js)} event(s), ~{_fmt_gb(sum(j.est_bytes() for j in js))}?", default=False):
            accepted.extend(js)
    done = failed = 0
    folders: set[Path] = {j.folder for j in jobs}
    rendered: list[Path] = []
    for j in accepted:
        console.print(f"[bold]{j.ev.id}[/bold] → {j.out_dir.relative_to(out)}")
        try:
            render_mod.render_job(root, j, model=model_for(j), log=console.print)
            done += 1
            rendered.append(j.out_dir / "mosaic.mp4")
        except Exception as e:      # per-event isolation; the receipt is only written on success
            failed += 1
            console.print(f"  [red]failed:[/red] {e}")
    kept_ids = {e.id for e in events if (decisions.get(e.id) or dec_mod.Decision()).keep == "keep"} | set(event)
    for folder in sorted(folders):
        if not folder.exists():
            continue
        stale = render_mod.stale_dirs(folder, kept_ids)
        if stale:
            console.print(f"[yellow]{len(stale)} rendered folder(s) in {folder.name} no longer Keep:[/yellow] "
                          + ", ".join(p.name for p in stale[:8]) + (" …" if len(stale) > 8 else ""))
            if prune and (yes or typer.confirm(f"delete these {len(stale)} folder(s)?", default=False)):
                import shutil
                for p in stale:
                    shutil.rmtree(p, ignore_errors=True)
                console.print(f"[dim]pruned {len(stale)}[/dim]")
        m = render_mod.write_manifest(folder, kept_ids, album=album)
        console.print(f"[dim]manifest {m}[/dim]")
    console.print(f"[green]{done} rendered[/green]" + (f" · [red]{failed} failed[/red]" if failed else ""))
    if open_result and rendered and sys.platform == "darwin":
        subprocess.run(["open", *map(str, rendered[:3])])
    if failed:
        raise typer.Exit(1)


def _parse_models(values: list[str]) -> tuple[str, dict[str, str]]:
    """['Model X', 'TeslaDump2023=Model Y'] → ('Model X', {'TeslaDump2023': 'Model Y'})."""
    default, by_dump = "", {}
    for v in values:
        if "=" in v:
            d, m = v.split("=", 1)
            by_dump[d.strip()] = m.strip()
        else:
            default = v.strip()
    return default, by_dump


if __name__ == "__main__":
    app()
