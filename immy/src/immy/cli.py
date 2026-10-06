from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

# Heavy submodules (torch/transformers/etc.) are loaded lazily on first
# attribute access so `immy audit` doesn't pay multi-second import cost
# for code paths it doesn't touch. Each name is a proxy that imports its
# real module on the first attribute lookup.
import importlib as _importlib


class _LazyModule:
    __slots__ = ("_name", "_mod")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mod", None)

    def _load(self):
        mod = object.__getattribute__(self, "_mod")
        if mod is None:
            mod = _importlib.import_module(
                "." + object.__getattribute__(self, "_name"), __package__
            )
            object.__setattr__(self, "_mod", mod)
        return mod

    def __getattr__(self, attr):
        return getattr(self._load(), attr)

    def __setattr__(self, attr, value):
        if attr in self.__slots__:
            object.__setattr__(self, attr, value)
            return
        setattr(self._load(), attr, value)

    def __delattr__(self, attr):
        if attr in self.__slots__:
            object.__delattr__(self, attr)
            return
        delattr(self._load(), attr)


apple_photos_mod = _LazyModule("apple_photos")
backfill_dates_mod = _LazyModule("backfill_dates")
bloat_mod = _LazyModule("bloat")
captions_mod = _LazyModule("captions")
clip_mod = _LazyModule("clip")
clustering_mod = _LazyModule("clustering")
duplicates_mod = _LazyModule("duplicates")
match_mod = _LazyModule("match")
offline_mod = _LazyModule("offline")
process_mod = _LazyModule("process")
promote_mod = _LazyModule("promote")
pg_mod = _LazyModule("pg")
schema_contract_mod = _LazyModule("schema_contract")
similar_mod = _LazyModule("similar")
snapshot_mod = _LazyModule("snapshot")
srt_mod = _LazyModule("srt")
srtgeo_mod = _LazyModule("srtgeo")
tagsync_mod = _LazyModule("tagsync")
track_mod = _LazyModule("track")
transcripts_mod = _LazyModule("transcripts")
trips_mod = _LazyModule("trips")
from . import config as config_mod
from .config import load as load_config
from .exif import has_valid_gps as has_gps, read_folder
from .immich import ImmichClient, ImmichError
from .notes import (
    ensure_notes,
    join_make_model as _join_make_model,
    parse_frontmatter,
    resolve as resolve_notes,
    update_frontmatter,
)
from .rules import Finding, evaluate, dedup_by_field as _dedup_by_field
from .rules.trip_timezone_guess import guess_timezone
from .sidecar import write as write_xmp
from .state import State, log_event, patch_hash

app = typer.Typer(
    help="Pre-ingest metadata forensics for trip folders.",
    no_args_is_help=True,
)
console = Console()


MAX_APPLY_PASSES = 3


def _finding_patch_hash(f: Finding) -> str:
    return patch_hash({"action": f.action, "patch": f.patch, "pair_with": str(f.pair_with)})


def _compute_pending(
    rows, folder: Path, state: State
) -> tuple[list[Finding], list[Finding], list[Finding], list[Finding]]:
    """Return (all_findings, pending_high, pending_medium, already_applied).

    HIGH and MEDIUM dedup in separate pools — a MEDIUM finding is still
    surfaced for user review even if a HIGH rule also claims the same
    XMP field (the MEDIUM tier's patch only wins if the user accepts it
    AND applies after HIGH has converged).
    """
    all_findings = _dedup_by_field(evaluate(rows, folder))
    pending_high: list[Finding] = []
    pending_medium: list[Finding] = []
    already: list[Finding] = []
    for f in all_findings:
        if f.confidence not in ("high", "medium"):
            continue
        rel = f.path.relative_to(folder).as_posix()
        if state.is_applied(rel, f.rule, _finding_patch_hash(f)):
            already.append(f)
        elif f.confidence == "high":
            pending_high.append(f)
        else:
            pending_medium.append(f)
    return all_findings, pending_high, pending_medium, already


def _apply_once(folder: Path, state: State, pending: list[Finding]) -> int:
    """Apply each finding, update state + log. Returns count applied."""
    for f in pending:
        rel = f.path.relative_to(folder).as_posix()
        if f.action == "write_xmp":
            write_xmp(f.path, f.patch)
        elif f.action == "write_notes":
            _apply_write_notes(f)
        state.mark_applied(rel, f.rule, _finding_patch_hash(f))
        log_event(folder, {
            "event": "applied",
            "rule": f.rule,
            "file": rel,
            "action": f.action,
            "patch": f.patch,
            "pair_with": str(f.pair_with) if f.pair_with else None,
        })
    state.save()
    return len(pending)


def _apply_write_notes(f: Finding) -> None:
    """Apply a note-edit patch. Supported keys:
    - `add_tags`: list → merge unique into front-matter `tags:`
    - `timezone`: string → set front-matter `timezone:` (used by
      trip-timezone-guess-gps)
    - `location_coords`: [lat, lon] → set front-matter `location.coords`
      (used by geocode-place)
    More keys can join here as write_notes rules grow."""
    updates: dict = {}
    add_tags = f.patch.get("add_tags") or []
    if add_tags:
        fm = parse_frontmatter(f.path)
        existing = fm.get("tags")
        merged: list = list(existing) if isinstance(existing, list) else []
        seen = set(merged)
        for t in add_tags:
            if t not in seen:
                merged.append(t)
                seen.add(t)
        updates["tags"] = merged
    tz = f.patch.get("timezone")
    if isinstance(tz, str) and tz.strip():
        updates["timezone"] = tz.strip()
    coords = f.patch.get("location_coords")
    if isinstance(coords, (list, tuple)) and len(coords) == 2:
        try:
            lat, lon = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            lat = lon = None
        if lat is not None and lon is not None:
            updates["location"] = {"coords": [lat, lon]}
    if not updates:
        return
    update_frontmatter(f.path, updates)


def _apply_loop(folder: Path, state: State, initial_pending: list[Finding]) -> int:
    """Apply, re-read, re-evaluate until fixed point or MAX_APPLY_PASSES.

    Handles rule dependencies (e.g. trip-timezone needs a date written by
    dji-date-from-srt in the same run). Each pass re-reads EXIF so a later
    rule sees earlier writes.
    """
    total = _apply_once(folder, state, initial_pending)
    for pass_n in range(2, MAX_APPLY_PASSES + 1):
        rows = read_folder(folder)
        _, pending, _, _ = _compute_pending(rows, folder, state)
        if not pending:
            break
        console.print(f"[dim]pass {pass_n}: {len(pending)} new finding(s) after re-read[/dim]")
        total += _apply_once(folder, state, pending)
    return total


def _first_present(row, *keys: str) -> tuple[object | None, str | None]:
    for key in keys:
        value = row.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value, key
    return None, None


def _fmt_date(row) -> str:
    value, source = _first_present(
        row,
        "XMP:DateTimeOriginal", "EXIF:DateTimeOriginal",
        "QuickTime:CreateDate", "EXIF:CreateDate",
    )
    if value is None:
        return "—"
    suffix = " (xmp)" if source and source.startswith("XMP:") else ""
    return f"{value}{suffix}"


def _fmt_gps(row) -> str:
    lat, lat_source = _first_present(
        row,
        "Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude",
    )
    lon, lon_source = _first_present(
        row,
        "Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude",
    )
    if lat is None or lon is None:
        return "—"
    suffix = ""
    if (
        lat_source and lon_source
        and lat_source.startswith("XMP:")
        and lon_source.startswith("XMP:")
    ):
        suffix = " (xmp)"
    return f"{float(lat):+.4f},{float(lon):+.4f}{suffix}"


def _fmt_make_model(row) -> str:
    make, _ = _first_present(
        row,
        "EXIF:Make", "QuickTime:Make", "QuickTime:AndroidMake",
    )
    model, _ = _first_present(
        row,
        "EXIF:Model", "QuickTime:Model", "QuickTime:AndroidModel",
    )
    s = _join_make_model(make, model)
    if s:
        return s

    hier = row.get("XMP:HierarchicalSubject")
    if isinstance(hier, list):
        for item in hier:
            if isinstance(item, str) and item.startswith("Gear/Camera/"):
                camera = item.removeprefix("Gear/Camera/").strip()
                if camera:
                    return f"{camera} (xmp)"

    return "—"


def _render_table(folder: Path, rows, findings_by_path: dict[str, list[Finding]]) -> None:
    table = Table(show_lines=False)
    table.add_column("file", overflow="fold")
    table.add_column("date")
    table.add_column("gps")
    table.add_column("camera")
    table.add_column("flags")
    for r in rows:
        flags = findings_by_path.get(str(r.path), [])
        flag_str = ",".join(f"{f.rule}" for f in flags) or "—"
        table.add_row(
            r.path.relative_to(folder).as_posix(),
            _fmt_date(r),
            _fmt_gps(r),
            _fmt_make_model(r),
            flag_str,
        )
    console.print(table)


def _parse_coords(raw: str) -> tuple[float, float] | None:
    parts = raw.replace(";", ",").split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return None


def _prompt_medium_findings(
    findings: list[Finding],
    *,
    yes_medium: bool,
    interactive: bool,
) -> list[Finding]:
    """Return the MEDIUM findings the user (implicitly or explicitly) accepts.

    - yes_medium → auto-accept all
    - interactive → one y/n per finding, except findings sharing a `group`
      key collapse into a single "apply to N file(s)?" prompt
    - else → accept none (they stay pending for a later run)
    """
    if not findings:
        return []
    if yes_medium:
        return list(findings)
    if not interactive:
        return []

    groups: dict[str, list[Finding]] = {}
    singletons: list[Finding] = []
    for f in findings:
        if f.group:
            groups.setdefault(f.group, []).append(f)
        else:
            singletons.append(f)

    accepted: list[Finding] = []
    total_prompts = len(groups) + len(singletons)
    console.print(f"\n[bold]{total_prompts} MEDIUM finding(s) need review[/bold]")

    for gkey, gfindings in groups.items():
        sample = gfindings[0]
        console.print(
            f"\n[yellow]?[/yellow] [bold]{sample.rule}[/bold] — "
            f"[cyan]{len(gfindings)} file(s)[/cyan]  ({gkey})"
        )
        if sample.reason:
            console.print(f"  reason: {sample.reason}")
        answer = typer.prompt("  apply to all? [y/N]", default="n", show_default=False).strip().lower()
        if answer in ("y", "yes"):
            accepted.extend(gfindings)
            console.print(f"  [green]✓[/green] accepted {len(gfindings)} file(s)")
        else:
            console.print("  [dim]skipped[/dim]")

    for f in singletons:
        rel = f.path.name
        console.print(
            f"\n[yellow]?[/yellow] [bold]{f.rule}[/bold] on [cyan]{rel}[/cyan]"
        )
        if f.reason:
            console.print(f"  reason: {f.reason}")
        if f.patch:
            patch_str = ", ".join(f"{k}={v}" for k, v in f.patch.items())
            console.print(f"  would write: {patch_str}")
        answer = typer.prompt("  apply? [y/N]", default="n", show_default=False).strip().lower()
        if answer in ("y", "yes"):
            accepted.append(f)
            console.print("  [green]✓[/green] accepted")
        else:
            console.print("  [dim]skipped[/dim]")
    return accepted


def _has_tz_suffix(s: object) -> bool:
    if not isinstance(s, str) or len(s) < 6:
        return False
    tail = s.strip()[-6:]
    return tail[0] in "+-" and tail[3] == ":"


def _prompt_trip_timezone(folder: Path, rows, notes: Path | None, interactive: bool) -> bool:
    """Ask for an IANA timezone when notes has none and some media have
    dates without a `±HH:MM` suffix. Validates via zoneinfo before writing.
    Returns True if notes were modified (caller re-evaluates)."""
    if not interactive or notes is None:
        return False
    fm = parse_frontmatter(notes)
    tz = fm.get("timezone")
    if isinstance(tz, str) and tz.strip():
        return False
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    naive = 0
    for r in rows:
        raw = r.get("XMP:DateTimeOriginal", "EXIF:DateTimeOriginal", "QuickTime:CreateDate")
        if raw and not _has_tz_suffix(raw):
            naive += 1
    if not naive:
        return False
    guessed = guess_timezone(rows, folder)
    if guessed is not None:
        zone, reason = guessed
        update_frontmatter(notes, {"timezone": zone})
        console.print(
            f"[green]✓[/green] inferred timezone '{zone}' from {reason} "
            f"and wrote it to {notes.name}"
        )
        return True
    console.print(
        f"\n[yellow]?[/yellow] {naive}/{len(rows)} file(s) have naive dates and "
        f"[cyan]{notes.name}[/cyan] has no [b]timezone:[/b] set."
    )
    raw_in = typer.prompt(
        "Enter IANA zone (e.g. Indian/Mauritius, Europe/Madrid, empty to skip)",
        default="",
        show_default=False,
    ).strip()
    if not raw_in:
        console.print("[dim]skipped — dates will stay naive[/dim]")
        return False
    try:
        ZoneInfo(raw_in)
    except ZoneInfoNotFoundError:
        console.print(f"[red]unknown zone '{raw_in}'; skipping[/red]")
        return False
    update_frontmatter(notes, {"timezone": raw_in})
    console.print(f"[green]✓[/green] wrote timezone '{raw_in}' to {notes.name}")
    return True


def _prompt_trip_coords(folder: Path, rows, notes: Path | None, interactive: bool) -> bool:
    """Ask the user for trip-wide GPS anchor when notes has no coords and
    some media lack GPS. Writes answer back to notes front-matter. Returns
    True if notes were modified (caller should re-evaluate)."""
    if not interactive or notes is None:
        return False
    fm = parse_frontmatter(notes)
    loc = fm.get("location") or {}
    if isinstance(loc, dict) and loc.get("coords"):
        return False
    gpsless = [r for r in rows if not has_gps(r)]
    if not gpsless:
        return False
    console.print(
        f"\n[yellow]?[/yellow] {len(gpsless)}/{len(rows)} file(s) lack GPS and "
        f"[cyan]{notes.name}[/cyan] has no [b]location.coords[/b]."
    )
    raw = typer.prompt(
        "Enter 'lat, lon' for the trip anchor (empty to skip)",
        default="",
        show_default=False,
    )
    if not raw.strip():
        console.print("[dim]skipped — LOW finding will remain pending[/dim]")
        return False
    coords = _parse_coords(raw)
    if coords is None:
        console.print(f"[red]could not parse '{raw}' as 'lat, lon'; skipping[/red]")
        return False
    lat, lon = coords
    update_frontmatter(notes, {"location": {"coords": [lat, lon]}})
    console.print(f"[green]✓[/green] wrote coords [{lat}, {lon}] to {notes.name}")
    return True


@app.command()
def audit(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    write: bool = typer.Option(False, "--write", help="Apply HIGH-confidence findings (default: read-only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --write, report but don't modify anything."),
    auto: bool = typer.Option(False, "--auto", help="Non-interactive: skip LOW prompts, never ask."),
    yes_medium: bool = typer.Option(False, "--yes-medium", help="Auto-accept MEDIUM findings (no per-finding prompt)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Per-file EXIF dump."),
) -> None:
    """Read EXIF, propose corrections, optionally write XMP sidecars."""
    console.print(f"[bold cyan]immy audit[/bold cyan] {folder}")
    rows = read_folder(folder)

    # Scaffold notes file BEFORE any interactive prompt, so the prompt has
    # a target to write into.
    created_notes = ensure_notes(folder, rows) if rows else None
    notes = resolve_notes(folder)

    console.print(
        f"[bold]{folder}[/bold] — {len(rows)} media file(s)"
        + (f"  notes: [cyan]{notes.name}[/cyan]" if notes else "  notes: [dim]none[/dim]")
    )
    if created_notes is not None:
        console.print(f"[green]created[/green] notes file: {created_notes.name}")

    if not rows:
        return

    # Interactive pre-flight for LOW questions that must be answered before
    # evaluation (today: the trip GPS anchor).
    interactive = not auto and not dry_run
    if _prompt_trip_coords(folder, rows, notes, interactive):
        pass  # notes updated; evaluate below will see the new coords
    if _prompt_trip_timezone(folder, rows, notes, interactive):
        pass  # notes updated; trip-timezone HIGH rule will fire on evaluate

    state = State.load(folder)
    all_findings, pending_high, pending_medium, already = _compute_pending(rows, folder, state)
    by_path: dict[str, list[Finding]] = {}
    for f in all_findings:
        by_path.setdefault(str(f.path), []).append(f)

    _render_table(folder, rows, by_path)

    if not all_findings:
        return

    console.print(
        f"\nHIGH findings: [green]{len(pending_high)}[/green] pending, "
        f"[dim]{len(already)}[/dim] already applied"
    )
    per_rule: dict[str, int] = {}
    for f in pending_high:
        per_rule[f.rule] = per_rule.get(f.rule, 0) + 1
    for rule, count in sorted(per_rule.items()):
        marker = "[yellow]would[/yellow]" if (dry_run or not write) else "[green]apply[/green]"
        console.print(f"  {marker} {rule}: {count} file(s)")

    if pending_medium:
        console.print(f"\nMEDIUM findings: [yellow]{len(pending_medium)}[/yellow] pending review")
        per_rule_m: dict[str, int] = {}
        for f in pending_medium:
            per_rule_m[f.rule] = per_rule_m.get(f.rule, 0) + 1
        for rule, count in sorted(per_rule_m.items()):
            console.print(f"  [yellow]review[/yellow] {rule}: {count} file(s)")

    if write and not dry_run:
        total_high = _apply_loop(folder, state, pending_high)
        console.print(f"[green]✓[/green] wrote {total_high} HIGH finding(s)")

        # Re-evaluate MEDIUM now that HIGH has converged — a HIGH write
        # (e.g. dji-date-from-srt) may have resolved what looked like drift.
        if pending_medium:
            rows = read_folder(folder)
            _, _, pending_medium, _ = _compute_pending(rows, folder, state)
            accepted = _prompt_medium_findings(
                pending_medium,
                yes_medium=yes_medium,
                interactive=not auto,
            )
            if accepted:
                total_med = _apply_loop(folder, state, accepted)
                console.print(f"[green]✓[/green] wrote {total_med} finding(s) (MEDIUM + cascade)")

    if verbose:
        for r in rows:
            rel = r.path.relative_to(folder).as_posix()
            console.print(f"\n[bold]{rel}[/bold]")
            for k, v in sorted(r.raw.items()):
                if k == "SourceFile":
                    continue
                console.print(f"  {k}: {v}")


def _require_live_schema(conn) -> None:
    """Abort (exit 2) before any direct DB write if the live Immich schema
    has drifted from what immy writes — see `schema_contract`."""
    try:
        schema_contract_mod.assert_live_schema(conn)
    except schema_contract_mod.SchemaMismatch as e:
        console.print(str(e), style="red", markup=False, highlight=False)
        conn.close()
        raise typer.Exit(code=2)


def _promote_schema_preflight(config) -> None:
    """`promote` writes `asset_file` rows, offline-cache replays and asset
    un-trash UPDATEs, each over its own connection that degrades softly when
    Postgres is down. Check the schema once, up front, before any rsync. An
    unreachable DB is left to those steps to report."""
    if config.pg is None or config.immich is None:
        return
    try:
        conn = pg_mod.connect(config.pg)
    except Exception:  # noqa: BLE001 — the write steps surface connectivity
        return
    _require_live_schema(conn)
    conn.close()


def _promote_impl(
    folder: Path,
    dry_run: bool,
    force: bool,
    config_path: Path | None,
    resurrect_deleted: bool = False,
    reembed: str = "none",
    into_album: str | None = None,
    tags: list[str] | None = None,
    verify: bool = False,
) -> None:
    """Rsync + Immich library-scan + Insta360 stack calls.

    Shared body for the `promote` / `push` / `pub` aliases. Exits 1 when any
    step failed (after every step that can still run has run), 130 on Ctrl-C.
    """
    config = load_config(config_path)
    if verify:
        _promote_verify(folder, config, into_album=into_album)
        return
    if config.originals_root is None:
        console.print(
            "[red]no originals_root configured[/red] — set `originals_root:` in "
            "~/.immy/config.yml (or $IMMY_CONFIG), or pass --config <file>."
        )
        raise typer.Exit(code=2)

    try:
        plan = promote_mod.build_plan(folder, config)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    console.print(
        f"[bold]promote[/bold] {folder}\n"
        f"  → {plan.target}\n"
        f"  pairs to stack: {len(plan.pairs)}\n"
        f"  HIGH pending: {plan.pending_high}"
    )

    if plan.pending_high and not force:
        console.print(
            f"[red]refusing[/red] — {plan.pending_high} HIGH finding(s) pending. "
            "Run `immy audit --write` first, or pass --force."
        )
        raise typer.Exit(code=1)

    if not dry_run:
        _promote_schema_preflight(config)

    client: ImmichClient | None = None
    if config.immich is not None and not dry_run:
        client = ImmichClient(
            url=config.immich.url,
            api_key=config.immich.api_key,
            ssh_host=config.immich.ssh_host,
        )
    elif config.immich is None:
        console.print("[dim]no immich creds — rsync only, no scan or stacks.[/dim]")

    try:
        summary = promote_mod.execute(
            plan, config, dry_run=dry_run, client=client,
            resurrect_deleted=resurrect_deleted, reembed=reembed,
            into_album=into_album, tags=tags,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/yellow] — rsync stopped; scan/stack/album skipped.")
        raise typer.Exit(code=130)

    step_failed = False
    prefix = "[yellow]dry-run[/yellow] " if dry_run else ""
    changed = len(summary["rsync_changes"])
    console.print(f"{prefix}rsync: {changed} change(s) to {summary['target']}")
    off = summary.get("offline_sync")
    if off:
        if "error" in off:
            step_failed = True
            console.print(
                f"[yellow]offline-sync:[/yellow] {off['pending']} pending; "
                f"[red]{off['error']}[/red]"
            )
        elif off.get("note"):
            console.print(
                f"[dim]offline-sync: {off['pending']} pending ({off['note']})[/dim]"
            )
        elif off["pending"] == 0:
            console.print(
                f"[dim]offline-sync: {off['total']} entry(ies), all synced[/dim]"
            )
        else:
            step_failed = step_failed or off["failed"] > 0
            colour = "green" if off["failed"] == 0 else "yellow"
            console.print(
                f"[{colour}]offline-sync:[/{colour}] synced {off['synced']} of "
                f"{off['pending']} pending"
                + (f", [red]{off['failed']} failed[/red]" if off["failed"] else "")
            )
    if summary.get("marker_warning"):
        console.print(f"[yellow]warning:[/yellow] {summary['marker_warning']}")
    if "scan_error" in summary:
        step_failed = True
        console.print(f"[red]scan failed:[/red] {summary['scan_error']}")
    elif summary["scan_triggered"]:
        console.print("[green]✓[/green] library scan triggered")
    elif summary.get("scan_skipped_reason") == "y_processed":
        console.print("[dim]scan skipped: y_processed.yml present[/dim]")
        derivs = summary.get("derivatives") or {}
        step_failed = step_failed or derivs.get("status") == "error"
        if derivs:
            colour = {
                "pushed": "green", "empty": "dim",
                "skipped": "dim", "error": "red",
            }.get(derivs.get("status", ""), "")
            console.print(
                f"derivatives [{colour}]{derivs['status']}[/{colour}] "
                f"{derivs['detail']}"
            )
    for status, detail in summary["stacks"]:
        step_failed = step_failed or status == "error"
        colour = {
            "stacked": "green", "planned": "yellow", "skipped": "dim", "error": "red",
        }.get(status, "")
        console.print(f"  [{colour}]{status}[/{colour}] {detail}")
    album = summary.get("album") or {}
    step_failed = step_failed or album.get("status") == "error"
    if album and album.get("status") != "skipped":
        colour = {
            "created": "green", "updated": "green", "error": "red",
        }.get(album.get("status", ""), "")
        suffix = ""
        if album.get("resurrected"):
            suffix += f" [yellow]({album['resurrected']} resurrected)[/yellow]"
        if album.get("missing"):
            suffix += f" [dim]({album['missing']} asset(s) not yet indexed)[/dim]"
        console.print(
            f"album [{colour}]{album['status']}[/{colour}] "
            f"{album['name']}: {album['detail']}{suffix}"
        )
    if album and album.get("trashed_skipped"):
        console.print(
            f"[yellow]note:[/yellow] {album['trashed_skipped']} asset(s) under this "
            "path are trashed (online soft-deletes, left as-is). "
            "Pass [bold]--resurrect-deleted[/bold] to include them."
        )
    if album and album.get("thumbs_repair_error"):
        step_failed = True
        console.print(
            f"[red]thumbnail repair failed:[/red] {album['thumbs_repair_error']}")
    tagsum = album.get("tags") if album else None
    if tagsum:
        if "error" in tagsum:
            step_failed = True
            console.print(f"[red]tags failed:[/red] {tagsum['error']}")
        else:
            applied = tagsum.get("applied", {})
            parts = ", ".join(f"{n}={v}" for n, v in applied.items())
            console.print(f"[green]✓[/green] tagged: {parts}")
    reembed = summary.get("reembed")
    if reembed:
        queued = [q for q in ("smartSearch", "faceDetection")
                  if reembed.get(q) == "queued"]
        if queued:
            console.print(
                f"[green]✓[/green] re-embed [{reembed['mode']}] queued: "
                f"{', '.join(queued)} (Immich processes async)"
            )
        for q in ("smartSearch", "faceDetection"):
            if str(reembed.get(q, "")).startswith("error"):
                step_failed = True
                console.print(f"[red]re-embed {q} failed:[/red] {reembed[q]}")
        if "check_error" in reembed:
            console.print(f"[yellow]re-embed check skipped:[/yellow] {reembed['check_error']}")
    if step_failed:
        raise typer.Exit(code=1)


_VERIFY_MAX_EXAMPLES = 20


def _promote_verify(folder: Path, config, *, into_album: str | None) -> None:
    """`promote --verify`: album asset count vs local media files. Read-only
    — no rsync, no scan, no DB writes. Exit 1 on mismatch."""
    if config.pg is None or config.immich is None:
        console.print("[red]--verify needs pg: and immich: blocks in immy config.[/red]")
        raise typer.Exit(code=2)
    client = ImmichClient(
        url=config.immich.url,
        api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    )
    try:
        res = promote_mod.verify_trip(folder, config, client, into_album=into_album)
    except Exception as e:  # noqa: BLE001 — DB/API unreachable
        console.print(f"[red]verify failed:[/red] {e}")
        raise typer.Exit(code=2)
    album_note = "" if res.album_found else " [red](album not found)[/red]"
    console.print(
        f"[bold]verify[/bold] {folder.name} → album {res.album}{album_note}\n"
        f"  album {res.album_count} asset(s), local {res.expected_count} file(s)"
    )
    if res.ok:
        console.print("[green]✓[/green] album matches the local trip")
        return
    console.print(
        f"[red]mismatch[/red] — {len(res.missing)} local file(s) not in the album, "
        f"{len(res.extra)} in the album but not local"
    )
    # Examples share one budget; missing first (the actionable direction).
    examples = [("missing", n) for n in res.missing] + [("extra", n) for n in res.extra]
    for kind, name in examples[:_VERIFY_MAX_EXAMPLES]:
        console.print(f"  {kind}: {name}", markup=False, highlight=False)
    if len(examples) > _VERIFY_MAX_EXAMPLES:
        console.print(f"  [dim]… and {len(examples) - _VERIFY_MAX_EXAMPLES} more[/dim]")
    raise typer.Exit(code=1)


def _promote(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the plan; no rsync, no API calls."),
    force: bool = typer.Option(False, "--force", help="Promote even if HIGH findings are still pending."),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
    resurrect_deleted: bool = typer.Option(False, "--resurrect-deleted", help="Also un-delete (clear deletedAt) assets under this trip path. Off by default so album sync never undoes a soft-delete you made in Immich."),
    reembed: str = typer.Option("none", "--reembed", help="After scan, trigger Immich CLIP+faces jobs (immy-inserted assets are NOT auto-queued). 'missing'=new assets only; 'all'=reprocess whole library (one-time stale-index cleanup); 'none'=off (default). LIBRARY-WIDE — in a batch, pass it once on the last trip, not per-trip."),
    into_album: str = typer.Option(None, "--into-album", help="Add this trip's assets to an EXISTING album of this name instead of one named after the folder (the merge case — e.g. promote ivan-photoshoot INTO anya-beach-photoshop). The target album's description is left untouched."),
    tag: list[str] = typer.Option(None, "--tag", help="Tag this trip's assets with this flat tag name (repeatable). Used to mark merged/edited files, e.g. --tag post-edited --tag with-anya. Idempotent."),
    verify: bool = typer.Option(False, "--verify", help="Read-only check instead of a promote: compare the Immich album's asset count (for this trip's path) with the local media files; list up to 20 missing names and exit 1 on mismatch. Never writes."),
) -> None:
    """Rsync trip into originals + trigger Immich scan + stack Insta360 pairs."""
    if reembed not in ("none", "missing", "all"):
        raise typer.BadParameter("--reembed must be one of: none, missing, all")
    _promote_impl(
        folder, dry_run=dry_run, force=force, config_path=config_path,
        resurrect_deleted=resurrect_deleted, reembed=reembed,
        into_album=into_album, tags=tag or None, verify=verify,
    )


# Register under three names — Typer has no native aliases, so we just
# attach the same callback to each command name.
for _name in ("promote", "push", "pub"):
    app.command(name=_name)(_promote)


# --- `immy bloat` subcommands ---------------------------------------------

srt_app = typer.Typer(
    help="DJI .SRT telemetry — track sidecars, durable geotag, channel probe.",
    no_args_is_help=True,
)


def _srt_pg_setup(config_path: Path | None):
    """Load config and open a (conn, library) pair for DB-touching srt
    commands. Exits with a clear message when pg/immich config is missing or
    unreachable — same contract as `process`."""
    config = load_config(config_path)
    if config.immich is None or config.pg is None:
        console.print(
            "[red]srt geotag/verify-channel need both `pg:` and "
            "`immich.library_id` in immy config.[/red]"
        )
        raise typer.Exit(code=2)
    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)
    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
    except LookupError as e:
        console.print(f"[red]{e}[/red]")
        conn.close()
        raise typer.Exit(code=2)
    return config, conn, library


@srt_app.command("track")
def srt_track(
    folders: list[Path] = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Trip folder(s) to scan for drone clips with sibling .SRT.",
    ),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Emit `<stem>.gpx` + `<stem>.track.json` for every media file with a
    DJI `.SRT` sibling. Writes through WritablePaths, so on the NAS the
    sidecars land under `sidecars_root`, never beside the :ro originals."""
    config = load_config(config_path)
    total = 0
    for folder in folders:
        paths = process_mod.resolve_writable_paths(
            folder,
            originals_root=config.originals_root,
            state_root=config.state_root,
            sidecars_root=config.sidecars_root,
        )
        for media in sorted(_iter_media_with_srt(folder)):
            frames = srt_mod.parse_track(srt_mod.find_sibling(media))
            if not frames:
                continue
            gpx = paths.gpx_path(media)
            tj = paths.track_json_path(media)
            track_mod.write_gpx(frames, gpx, name=media.stem)
            track_mod.write_json(frames, tj)
            fixes = sum(1 for f in frames if f.has_fix())
            console.print(
                f"  {media.name}: {len(frames)} frames, {fixes} fixes "
                f"→ {gpx.name}, {tj.name}"
            )
            total += 1
    console.print(f"\n[green]wrote track sidecars for {total} clip(s)[/green]")


def _iter_media_with_srt(folder: Path):
    """Yield media files under `folder` that have a sibling DJI `.SRT`."""
    for row in read_folder(folder):
        if srt_mod.find_sibling(row.path) is not None:
            yield row.path


@srt_app.command("geotag")
def srt_geotag(
    folders: list[Path] = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Trip folder(s) whose drone clips need GPS from their .SRT.",
    ),
    write: bool = typer.Option(
        False, "--write",
        help="Apply the durable DB write (default: dry-run report).",
    ),
    relock: bool = typer.Option(
        False, "--relock",
        help="Also repair clips that already have DB coords but were never "
             "locked (fragile — a metadata refresh can wipe them) and never "
             "geocoded. Only touches rows whose DB coord matches the SRT "
             "fix within 2km; anything else is left alone as likely "
             "user-set.",
    ),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Write each drone clip's first valid fix (takeoff point) into Immich's
    `asset_exif.latitude/longitude` via the durable, lock-protected channel,
    so a metadata refresh can't clobber it, AND reverse-geocode it (country/
    state/city) from Immich's own geodata. Idempotent: assets already
    carrying DB coords are skipped (unless --relock)."""
    config, conn, library = _srt_pg_setup(config_path)
    tagged = would = no_asset = relocked = would_relock = mismatch = 0
    try:
        for folder in folders:
            console.print(f"\n[bold]srt geotag[/bold] {folder}")
            rows = read_folder(folder)
            outcomes = srtgeo_mod.geotag_folder(
                conn, library, folder, rows,
                write=write, relock=relock,
                emit=lambda m: console.print(m, highlight=False),
            )
            if write:
                conn.commit()
            f_tagged = sum(1 for o in outcomes if o.status == "tagged")
            f_would = sum(1 for o in outcomes if o.status == "would-tag")
            f_no_asset = sum(1 for o in outcomes if o.status == "no-asset")
            f_relocked = sum(1 for o in outcomes if o.status == "relocked")
            f_would_relock = sum(
                1 for o in outcomes if o.status == "would-relock")
            f_mismatch = sum(1 for o in outcomes if o.status == "skip-mismatch")
            tagged += f_tagged; would += f_would; no_asset += f_no_asset
            relocked += f_relocked; would_relock += f_would_relock
            mismatch += f_mismatch
            console.print(
                f"  [dim]{folder.name}: tagged={f_tagged} would-tag={f_would} "
                f"no-asset={f_no_asset} relocked={f_relocked} "
                f"would-relock={f_would_relock} skip-mismatch={f_mismatch}"
                f"[/dim]"
            )
    finally:
        conn.close()
    if write:
        console.print(
            f"\n[green]tagged {tagged} clip(s), relocked {relocked}[/green] "
            f"[dim](no-asset={no_asset}, skip-mismatch={mismatch})[/dim]"
        )
    else:
        console.print(
            f"\n[green]would tag {would} clip(s), would relock "
            f"{would_relock}[/green] [dim](no-asset={no_asset}, "
            f"skip-mismatch={mismatch}; run with --write to apply)[/dim]"
        )


@srt_app.command("geocode")
def srt_geocode(
    folder: Path = typer.Argument(
        None, file_okay=False, resolve_path=True,
        help="Trip folder; its name + library import path form the scope.",
    ),
    prefix: str = typer.Option(
        None, "--prefix",
        help="Scope by raw asset.originalPath prefix instead of a folder "
             "(DB-only backfill — no files needed).",
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply (default: dry-run report)."),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Backfill country/state/city for clips we geotagged but Immich won't
    geocode (locked coords / read-only originals), using Immich's own geodata
    so place names match the rest of the library. Operates purely on DB coords
    — pass a folder, or `--prefix` for assets whose files aren't mounted."""
    config, conn, library = _srt_pg_setup(config_path)
    if prefix is None and folder is None:
        console.print("[red]pass a folder or --prefix[/red]")
        conn.close(); raise typer.Exit(code=2)
    scope = prefix if prefix is not None else (
        f"{library.container_root}/{folder.name}/")
    console.print(f"[bold]srt geocode[/bold] scope={scope}")
    try:
        n = srtgeo_mod.geocode_located_missing(
            conn, scope, write=write,
            emit=lambda m: console.print(m, highlight=False))
        if write:
            conn.commit()
    finally:
        conn.close()
    verb = "geocoded" if write else "would geocode"
    console.print(f"\n[green]{verb} {n} clip(s)[/green]")


@srt_app.command("verify-channel")
def srt_verify_channel(
    asset: str = typer.Argument(
        ..., help="Asset UUID, or an originalFileName to resolve via the API "
                  "(use a drone VIDEO already in Immich).",
    ),
    refresh_wait: float = typer.Option(
        40.0, "--refresh-wait",
        help="Seconds to wait for the metadata-refresh job to land.",
    ),
    lock_tokens: str = typer.Option(
        "latitude,longitude", "--lock-tokens",
        help="Comma-separated lockedProperties tokens to test for the lock.",
    ),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Empirically prove which DB write survives an Immich metadata refresh
    for a VIDEO: write a sentinel coord unlocked vs. locked, trigger
    refresh-metadata, and report which survived. Restores the asset after."""
    config, conn, library = _srt_pg_setup(config_path)
    client = ImmichClient(
        url=config.immich.url,
        api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    )
    asset_id = asset if srtgeo_mod.is_uuid(asset) else client.find_asset_id(asset)
    if not asset_id:
        console.print(f"[red]could not resolve asset:[/red] {asset}")
        conn.close()
        raise typer.Exit(code=2)
    tokens = tuple(t.strip() for t in lock_tokens.split(",") if t.strip())
    console.print(
        f"[bold]verify-channel[/bold] asset={asset_id} "
        f"lock_tokens={tokens} wait={refresh_wait}s"
    )
    try:
        results = srtgeo_mod.verify_channel(
            conn, client, asset_id,
            refresh_wait_s=refresh_wait, lock_tokens=tokens,
        )
    finally:
        conn.close()
    table = Table(title="channel survival after refresh-metadata")
    table.add_column("channel"); table.add_column("survived")
    table.add_column("final coords"); table.add_column("lockedProperties after")
    for r in results:
        table.add_row(
            r.channel,
            "[green]YES[/green]" if r.survived else "[red]no[/red]",
            f"{r.final[0]}, {r.final[1]}",
            ", ".join(r.locked_after) or "—",
        )
    console.print(table)
    winner = next((r.channel for r in results if r.survived), None)
    if winner:
        console.print(f"[green]→ durable channel: {winner}[/green]")
    else:
        console.print(
            "[yellow]neither channel survived — try different --lock-tokens "
            "or inspect Immich's lockedProperties enum.[/yellow]"
        )


app.add_typer(srt_app, name="srt")


tags_app = typer.Typer(
    help="Native Immich Tag API push — the video-safe channel for "
         "notes-derived tags XMP can't reach.",
    no_args_is_help=True,
)


@tags_app.command("sync")
def tags_sync(
    folders: list[Path] = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Trip folder(s) whose notes `tags:` should be pushed.",
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply (default: dry-run report).",
    ),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Push each trip's notes `tags:` (Gear/Camera/*, Events/*, Source/*, …)
    to every one of its assets via Immich's native Tag API — the durable
    channel for video assets, which never pick up the XMP sidecar
    `trip-tags-from-notes` writes. Idempotent: re-running just re-asserts
    the same tags."""
    config, conn, library = _srt_pg_setup(config_path)
    client = ImmichClient(
        url=config.immich.url,
        api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    )
    tagged = would = no_asset = tag_failed = folder_errors = 0
    try:
        for folder in folders:
            console.print(f"\n[bold]tags sync[/bold] {folder}")
            # A transport/API error on one trip must not abort the rest of a
            # multi-trip run (this command is routinely run over the whole
            # library) — isolate per-folder like `promote` does per-asset.
            try:
                outcomes = tagsync_mod.tag_sync_folder(
                    conn, client, library, folder,
                    write=write, emit=lambda m: console.print(m, highlight=False),
                )
            except ImmichError as e:
                folder_errors += 1
                console.print(f"  [red]error:[/red] {e}")
                continue
            if write:
                conn.commit()
            f_tagged = sum(1 for o in outcomes if o.status == "tagged")
            f_would = sum(1 for o in outcomes if o.status == "would-tag")
            f_no_asset = sum(1 for o in outcomes if o.status == "no-asset")
            f_tag_failed = sum(1 for o in outcomes if o.status == "tag-failed")
            tagged += f_tagged; would += f_would; no_asset += f_no_asset
            tag_failed += f_tag_failed
            if not outcomes:
                console.print(
                    "  [yellow]no `tags:` in this trip's notes — "
                    "nothing to sync[/yellow]")
            else:
                console.print(
                    f"  [dim]{folder.name}: tagged={f_tagged} "
                    f"would-tag={f_would} no-asset={f_no_asset} "
                    f"tag-failed={f_tag_failed}[/dim]"
                )
    finally:
        conn.close()
    if folder_errors:
        console.print(f"[red]{folder_errors} trip(s) errored — see above[/red]")
    if write:
        console.print(
            f"\n[green]tagged {tagged} asset(s)[/green] "
            + (f"[red]tag-failed={tag_failed}[/red]" if tag_failed else "")
        )
        if tag_failed or folder_errors:
            raise typer.Exit(code=1)
    else:
        console.print(
            f"\n[green]would tag {would} asset(s)[/green] "
            f"[dim](no-asset={no_asset}; run with --write to apply)[/dim]"
        )
        if folder_errors:
            raise typer.Exit(code=1)


@tags_app.command("camera")
def tags_camera(
    folders: list[Path] = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Trip folder(s) whose notes-derived camera model should backfill "
             "asset_exif.make/model.",
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply (default: dry-run report).",
    ),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Backfill the Immich Details panel's blank "Camera" row via
    `devices.resolve` (the same owner-confirmed friendly-name table `immy
    process` uses at ingest) — never a raw module code like "FC8282". Falls
    back to the trip's notes `Gear/Camera/<code>` tag, itself resolved
    through the same table, only when the file carries no usable EXIF/
    Encoder signal at all (the common DJI-video case). Self-corrects an
    asset it already wrote if the resolved value has since changed (e.g.
    the friendly-name table gained an entry); never touches a value Immich
    itself extracted from the file."""
    config, conn, library = _srt_pg_setup(config_path)
    written = corrected = would = no_asset = no_signal = 0
    try:
        for folder in folders:
            console.print(f"\n[bold]tags camera[/bold] {folder}")
            outcomes = tagsync_mod.camera_sync_folder(
                conn, library, folder,
                write=write, emit=lambda m: console.print(m, highlight=False),
            )
            if write:
                conn.commit()
            f_written = sum(1 for o in outcomes if o.status == "written")
            f_corrected = sum(1 for o in outcomes if o.status == "corrected")
            f_would = sum(
                1 for o in outcomes if o.status in ("would-write", "would-correct"))
            f_no_asset = sum(1 for o in outcomes if o.status == "no-asset")
            f_no_signal = sum(1 for o in outcomes if o.status == "no-signal")
            written += f_written; corrected += f_corrected; would += f_would
            no_asset += f_no_asset; no_signal += f_no_signal
            if not outcomes:
                console.print(
                    "  [yellow]nothing resolvable — no file-level camera "
                    "signal and no notes gear tag[/yellow]")
            else:
                console.print(
                    f"  [dim]{folder.name}: written={f_written} "
                    f"corrected={f_corrected} would-write/correct={f_would} "
                    f"no-asset={f_no_asset} no-signal={f_no_signal}[/dim]"
                )
    finally:
        conn.close()
    if write:
        console.print(
            f"\n[green]wrote camera for {written} asset(s), "
            f"corrected {corrected}[/green]"
        )
    else:
        console.print(
            f"\n[green]would write/correct camera for {would} asset(s)[/green] "
            f"[dim](no-asset={no_asset}; run with --write to apply)[/dim]"
        )


app.add_typer(tags_app, name="tags")


bloat_app = typer.Typer(
    help="Phase 2c — detect oversized deliveries, batch-confirm, HEVC transcode.",
    no_args_is_help=True,
)


def _print_bloat_groups(folder: Path, candidates: list[bloat_mod.BloatCandidate]) -> None:
    if not candidates:
        console.print("[dim]no bloat candidates.[/dim]")
        return

    groups = bloat_mod.group_by_folder(candidates, folder)
    total_current = sum(c.current_size for c in candidates)
    total_saved = sum(c.savings_bytes for c in candidates)

    console.print(
        f"\n[bold]{len(candidates)} candidate(s) across {len(groups)} folder(s)[/bold]  "
        f"total: {bloat_mod.fmt_bytes(total_current)}  "
        f"would save: [green]{bloat_mod.fmt_bytes(total_saved)}[/green] "
        f"({100 * total_saved / max(total_current, 1):.0f} %)"
    )

    for group_path, items in groups.items():
        g_size = sum(c.current_size for c in items)
        g_save = sum(c.savings_bytes for c in items)
        label = str(group_path) if str(group_path) != "." else "(root)"
        console.print(
            f"\n[bold]{label}[/bold] — {len(items)} file(s), "
            f"{bloat_mod.fmt_bytes(g_size)} → "
            f"save [green]{bloat_mod.fmt_bytes(g_save)}[/green] "
            f"({100 * g_save / max(g_size, 1):.0f} %)"
        )
        for c in items:
            console.print(
                f"  {c.path.name}  {c.width}x{c.height}@{c.fps:g}  "
                f"{c.codec_family} {bloat_mod.fmt_bitrate(c.current_bitrate)} → "
                f"hevc {bloat_mod.fmt_bitrate(c.target_bitrate)}  "
                f"[dim]({c.tier}, save {bloat_mod.fmt_bytes(c.savings_bytes)})[/dim]"
            )


@bloat_app.command("list")
def bloat_list(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
) -> None:
    """Walk folder, group bloat candidates by parent dir, print savings summary."""
    candidates = bloat_mod.scan(folder)
    _print_bloat_groups(folder, candidates)


@bloat_app.command("transcode")
def bloat_transcode(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    apply: bool = typer.Option(
        False, "--apply",
        help="After verify, atomic-replace originals (keeps <name>.original + receipt JSON).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report groups + ffmpeg plan; run no ffmpeg, make no changes.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip per-group confirmation (still groups by folder for progress output).",
    ),
) -> None:
    """Per-folder y/n confirm, then `hevc_videotoolbox` to `.optimized.<ext>`.

    Non-destructive by default — `--apply` does the atomic swap after verify.
    """
    candidates = bloat_mod.scan(folder)
    if not candidates:
        console.print("[dim]no bloat candidates.[/dim]")
        return

    _print_bloat_groups(folder, candidates)

    groups = bloat_mod.group_by_folder(candidates, folder)
    accepted: list[bloat_mod.BloatCandidate] = []
    for group_path, items in groups.items():
        if yes:
            accepted.extend(items)
            continue
        label = str(group_path) if str(group_path) != "." else "(root)"
        g_save = sum(c.savings_bytes for c in items)
        answer = typer.prompt(
            f"\ntranscode {label} ({len(items)} file(s), save "
            f"{bloat_mod.fmt_bytes(g_save)})? [y/N]",
            default="n",
            show_default=False,
        ).strip().lower()
        if answer in ("y", "yes"):
            accepted.extend(items)

    if not accepted:
        console.print("[dim]nothing accepted.[/dim]")
        return

    if dry_run:
        console.print(
            f"[yellow]dry-run[/yellow] would transcode {len(accepted)} file(s)"
        )
        return

    done: list[tuple[bloat_mod.BloatCandidate, Path]] = []
    for c in accepted:
        console.print(f"→ {c.path.relative_to(folder).as_posix()}")
        try:
            out = bloat_mod.transcode_one(c)
        except bloat_mod.TranscodeError as e:
            console.print(f"  [red]failed:[/red] {e}")
            continue
        console.print(
            f"  [green]✓[/green] {out.name}  "
            f"({bloat_mod.fmt_bytes(out.stat().st_size)})"
        )
        done.append((c, out))

    if apply:
        for c, out in done:
            try:
                receipt = bloat_mod.apply_one(c, out)
            except bloat_mod.TranscodeError as e:
                console.print(f"  [red]apply failed:[/red] {e}")
                continue
            console.print(
                f"  [green]applied[/green] {c.path.name}  "
                f"(receipt {receipt.name})"
            )
    else:
        console.print(
            f"[dim]wrote {len(done)} .optimized file(s); "
            f"re-run with --apply to atomic-replace originals.[/dim]"
        )


@bloat_app.command("sample")
def bloat_sample(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    frames: int = typer.Option(
        5, "--frames", "-n",
        help="How many evenly-spaced frames to extract per pair (default: 5, at 10/30/50/70/90%).",
    ),
    review_dir: Path = typer.Option(
        None, "--review-dir",
        help="Where to write frames + review.md (default: <folder>/.audit/bloat-review/).",
    ),
) -> None:
    """Extract matched frames from every `*.optimized.*` pair + compute PSNR.

    Run *before* `--apply` to eyeball whether the HEVC transcode kept
    enough quality. Produces `<folder>/.audit/bloat-review/review.md`
    with one section per pair — inline JPEG thumbs + overall PSNR —
    plus per-file verdict (`ok` / `review` / `fail`) based on the PSNR
    band. Most Markdown viewers (VS Code, Obsidian, Typora) render the
    thumbs inline so you can flip through it without a bespoke viewer.

    No effect on files: optimized versions stay as `.optimized.ext`,
    originals untouched. When you're satisfied, run
    `immy bloat transcode <folder> --apply` to atomic-replace.
    """
    # Find all optimized / source pairs under `folder`. `glob` works
    # fine because optimized files live next to their source (per
    # `optimized_path()` convention) — no subfolder juggling needed.
    pairs: list[tuple[Path, Path]] = []
    for opt in sorted(folder.rglob("*.optimized.*")):
        if not opt.is_file():
            continue
        src = bloat_mod.source_for_optimized(opt)
        if not src.is_file():
            console.print(
                f"[yellow]skip[/yellow] {opt.relative_to(folder)}: "
                f"source {src.name} missing"
            )
            continue
        pairs.append((src, opt))

    if not pairs:
        console.print(
            f"[dim]no `*.optimized.*` files under {folder} — "
            f"run `immy bloat transcode` first.[/dim]"
        )
        return

    if review_dir is None:
        review_dir = folder / ".audit" / "bloat-review"

    # Evenly-spaced percentages excluding 0/100 so black-padded frames
    # at video ends don't inflate numeric scores.
    if frames <= 0:
        console.print("[red]--frames must be positive[/red]")
        raise typer.Exit(code=2)
    step = 100.0 / (frames + 1)
    percents = tuple(int(round((i + 1) * step)) for i in range(frames))

    console.print(
        f"[bold]bloat sample[/bold] — {len(pairs)} pair(s), "
        f"frames at {','.join(f'{p}%' for p in percents)}"
    )
    reports: list[bloat_mod.SampleReport] = []
    for src, opt in pairs:
        console.print(f"  {src.relative_to(folder)} → sampling…")
        report = bloat_mod.sample_pair(
            src, opt, review_dir, percents=percents,
        )
        reports.append(report)
        psnr_str = (
            f"{report.psnr_db:.2f} dB" if report.psnr_db is not None else "—"
        )
        colour = {"ok": "green", "review": "yellow", "fail": "red"}.get(
            report.verdict, "dim",
        )
        console.print(
            f"    [{colour}]{report.verdict}[/{colour}] "
            f"psnr={psnr_str}  "
            f"[dim]({len(report.frames)} frame pair(s))[/dim]"
        )

    md = review_dir / "review.md"
    bloat_mod.render_review_md(reports, md)
    console.print(f"\n[green]✓[/green] wrote {md.relative_to(folder)}")


app.add_typer(bloat_app, name="bloat")


# --- `immy process` (Phase Y.1) -------------------------------------------


def _resolve_offline_library(
    folder: Path, config=None,
) -> tuple[object | None, bool]:
    """Return (library, recovered_from_marker) for offline mode.

    Checks global cache, then tries to recover container_root from any
    marker under `folder` or its siblings. Owner/library UUIDs stay as
    placeholders; sync-offline fills them in at push time. Markers are read
    where process wrote them (`WritablePaths.marker_path`: state_root on the
    NAS, `<trip>/.audit` on the Mac).
    """
    library = offline_mod.load_cached_library()
    if library is not None:
        return library, False

    def _marker_for(trip: Path) -> Path:
        return process_mod.resolve_writable_paths(
            trip,
            originals_root=getattr(config, "originals_root", None),
            state_root=getattr(config, "state_root", None),
            sidecars_root=getattr(config, "sidecars_root", None),
        ).marker_path

    root = offline_mod.derive_container_root_from_marker(
        folder, marker=_marker_for(folder))
    if root is None and folder.parent.is_dir():
        derived = offline_mod.derive_library_from_any_trip(
            folder.parent, marker_for=_marker_for)
        if derived is not None:
            return derived, True
    elif root is not None:
        from .pg import LibraryInfo as _LI
        return _LI(
            id="__offline_placeholder__",
            owner_id="__offline_placeholder__",
            container_root=root,
        ), True
    return None, False


def _run_one_trip(
    folder: Path,
    *,
    library,
    conn,
    offline: bool,
    recovered_from_marker: bool,
    dry_run: bool,
    compute: bool,
    compute_clip: bool,
    compute_faces: bool,
    with_transcripts: bool,
    with_captions: bool,
    recaption: bool,
    caption_fill_missing_only: bool,
    transcode_videos: bool,
    captioner_config,
    caption_workers: int,
    clip_model: str,
    clip_backend: str = "mlx",
    allow_mlx_clip: bool = False,
    clip_endpoint: str | None = None,
    transcript_model: str,
    transcript_prompt: str | None,
    transcript_backend: str = "mlx",
    transcript_endpoint: str | None = None,
    originals_root: Path | None = None,
    state_root: Path | None = None,
    sidecars_root: Path | None = None,
    force: bool = False,
    provenance: dict | None = None,
) -> bool:
    """Run the full pipeline for one trip folder. Returns True on success.

    `provenance` (db / mode / steps, see `process.marker_provenance`) is
    recorded in the marker and must match it for the cached-trip skip.

    Per-trip sink + commit boundary: a failure (or KeyboardInterrupt) in
    one trip rolls back only that trip's writes, so sibling trips already
    committed are durable. The caller handles Ctrl-C by letting it
    propagate out — we rollback in `finally` regardless.
    """
    # Resolve every writable target once. Unset roots → `<trip>/.audit` +
    # sidecars beside the media (Mac path, byte-identical). NAS passes
    # state_root/sidecars_root so nothing is written under the :ro originals.
    paths = process_mod.resolve_writable_paths(
        folder,
        originals_root=originals_root,
        state_root=state_root,
        sidecars_root=sidecars_root,
    )

    # Cheap skip: if the trip was fully processed previously and no source
    # file has been touched since, don't fork exiftool over thousands of
    # files just to re-confirm everything is cached. One stat() per file vs.
    # an exiftool spawn — saves ~all the wall-clock on a "scan all trips"
    # batch when most trips are already done. Pass --force to override;
    # --recaption is an explicit redo and bypasses it too.
    if not dry_run and not force and not recaption:
        cached, count = process_mod.is_trip_fully_cached(
            folder, marker=paths.marker_path, provenance=provenance)
        if cached:
            console.print(
                f"\n[dim][cached][/dim] {folder.name}: "
                f"{count} file(s) unchanged since marker — skipping"
            )
            return True

    if offline:
        sink: offline_mod.Sink = offline_mod.OfflineSink(
            folder, library, offline_root=paths.offline_dir)
    else:
        sink = offline_mod.PgSink(conn)

    hint = ""
    if offline and recovered_from_marker:
        hint = " [dim](owner_id/library_id pulled at sync time)[/dim]"
    console.print(
        f"\n[bold]process[/bold] {folder}"
        + (f"\n  [yellow]offline mode[/yellow]{hint}" if offline else "")
        + f"\n  target prefix:  {library.container_root}/{folder.name}/..."
    )

    if dry_run:
        from .exif import read_folder as _read
        from . import dji as _dji
        from . import raw as _raw
        rows = _read(folder, paths=paths)
        # Match process_trip's filter: all DJI `.LRF` proxies are
        # dropped (paired ones are consumed as ffmpeg input for their
        # master; orphans are stray low-res proxies) — never ingested as
        # standalone assets. Same for camera-baked JPEG previews next to
        # a sibling RAW (DJI DNG+JPG, Sony ARW+JPG, …).
        rows = [r for r in rows if not _dji.is_proxy(r.path)]
        _raw_idx = _raw.build_raw_index(r.path for r in rows)
        rows = [r for r in rows if not _raw.is_paired_preview(r.path, _raw_idx)]
        console.print(f"[yellow]dry-run[/yellow] would process {len(rows)} file(s)")
        for r in rows[:5]:
            asset, _ = process_mod.build_rows(r.path, folder, r, library)
            console.print(
                f"  {asset.asset_type:<5} {asset.original_path} "
                f"[dim]cs={asset.checksum.hex()[:12]}…[/dim]"
            )
        if len(rows) > 5:
            console.print(f"  [dim]… and {len(rows) - 5} more[/dim]")
        sink.close()
        return True

    def _progress(msg: str) -> None:
        console.print(msg, highlight=False, markup=False)

    try:
        results = process_mod.process_trip(
            folder, conn, library,
            sink=sink,
            compute_derivatives=compute,
            compute_clip=compute_clip,
            compute_faces=compute_faces,
            compute_transcripts=with_transcripts,
            compute_captions=with_captions,
            recaption=recaption,
            caption_fill_missing_only=caption_fill_missing_only,
            captioner_config=captioner_config,
            caption_workers=caption_workers,
            transcode_videos=transcode_videos,
            clip_model=clip_model,
            clip_backend=clip_backend,
            clip_endpoint=clip_endpoint,
            allow_mlx_clip=allow_mlx_clip,
            transcript_model=transcript_model,
            transcript_prompt=transcript_prompt,
            transcript_backend=transcript_backend,
            transcript_endpoint=transcript_endpoint,
            progress=_progress,
            paths=paths,
            force=force,
        )
        # process_trip commits per asset by default (commit_per_asset=True),
        # so the trip-level commit here is a defensive no-op for the
        # zero-asset case. Anything in-flight at Ctrl-C is rolled back below.
    except KeyboardInterrupt:
        sink.rollback()
        sink.close()
        raise
    except Exception as e:
        sink.rollback()
        sink.close()
        console.print(f"[red]{folder.name} failed, rolled back:[/red] {e}")
        return False
    finally:
        # sink.close is a no-op if already closed.
        try:
            sink.close()
        except Exception:
            pass

    new_count = sum(1 for r in results if r.inserted)
    existed = len(results) - new_count
    derivs = sum(len(r.derivatives) for r in results if r.derivatives)
    clipped = sum(1 for r in results if r.clip_embedded)
    face_count = sum(r.faces_detected for r in results)
    transcript_count = sum(1 for r in results if r.transcript)
    caption_count = sum(1 for r in results if r.caption)
    # Record only the steps that completed for every asset: a failed step
    # left in the marker would make the next unchanged run skip the trip.
    marker_prov = (
        None if provenance is None
        else process_mod.provenance_for_completed(provenance, results)
    )
    process_mod.write_marker(
        folder, results, marker=paths.marker_path, provenance=marker_prov)
    unfinished = sorted({s for r in results for s in r.incomplete_steps})
    if unfinished:
        console.print(
            f"[yellow]{folder.name}: unfinished step(s) {', '.join(unfinished)}"
            "[/yellow] — not recorded as done; the next run retries them"
        )
    tail = f", [cyan]{derivs} derivative file(s) staged[/cyan]" if derivs else ""
    tail += f", [cyan]{clipped} CLIP embedding(s)[/cyan]" if clipped else ""
    tail += f", [cyan]{face_count} face(s)[/cyan]" if face_count else ""
    tail += f", [cyan]{transcript_count} transcript(s)[/cyan]" if transcript_count else ""
    tail += f", [cyan]{caption_count} caption(s)[/cyan]" if caption_count else ""
    console.print(
        f"[green]✓[/green] {folder.name}: {new_count} new asset(s), "
        f"[dim]{existed} already present[/dim]{tail}"
    )
    return True


@app.command()
def process(
    folders: list[Path] = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="One or more trip folders. Multiple folders share a single "
             "process so MLX/Whisper/InsightFace models load only once.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report would-insert rows; no DB writes."),
    with_derivatives: bool = typer.Option(
        True, "--with-derivatives/--no-derivatives",
        help="Y.2 — stage thumbnail + preview under .audit/derivatives/ (default on).",
    ),
    with_clip: bool = typer.Option(
        True, "--with-clip/--no-clip",
        help="Y.3 — compute CLIP embedding on the staged preview, upsert smart_search (default on, requires --with-derivatives).",
    ),
    with_faces: bool = typer.Option(
        True, "--with-faces/--no-faces",
        help="Y.4 — Vision face detection + ArcFace embeddings, write asset_face + face_search (default on, requires --with-derivatives).",
    ),
    with_transcripts: bool = typer.Option(
        False, "--with-transcripts/--no-transcripts",
        help="Phase 3 — mlx-whisper per video: write <stem>.<lang>.srt next to source, store excerpt in asset_exif.description. Off by default (slow).",
    ),
    with_captions: bool = typer.Option(
        False, "--with-captions/--no-captions",
        help="Phase 3b — VLM caption per image via OpenAI-compat endpoint (LM Studio / OpenAI / Anthropic / Gemini). Writes 'AI: ...' into asset_exif.description. Configured under `ml.captioner` in config.yml. Off by default (costs tokens on cloud backends).",
    ),
    allow_mlx_clip: bool = typer.Option(
        False, "--allow-mlx-clip",
        help="Permit the mlx CLIP backend to write smart_search. Off by default: mlx vectors are only ~0.925 cosine to Immich's own and would split the shared index (also settable as ml.allow_mlx_clip in config.yml).",
    ),
    recaption: bool = typer.Option(
        False, "--recaption",
        help="Re-caption images that already have an AI: description (default: skip — saves ~9.5 s/image on a resumed overnight run).",
    ),
    captions_fill_missing: bool = typer.Option(
        False, "--captions-fill-missing",
        help="Caption only assets with NO caption yet (any model). Keeps captions made by a previous model id intact across a captioner-model bump, instead of redoing them. Ignored under --recaption.",
    ),
    caption_workers: int = typer.Option(
        1, "--caption-workers",
        help="Concurrent VLM caption requests per trip (default 1 = sequential). >1 fans the HTTP calls across a thread pool — LM Studio batches them, filling the GPU idle gaps between images (~1.3x at 2, ~1.6x at 3). Only the caption call is parallelized; derivatives/CLIP/faces stay sequential.",
    ),
    transcode_videos: bool = typer.Option(
        True, "--transcode/--no-transcode",
        help="Y.5 — emit a web-playable mp4 (libx264 720p, CRF 23) when the source isn't already h264/aac/mp4 ≤720p. Off → source plays only if the browser supports it.",
    ),
    offline: bool = typer.Option(
        False, "--offline",
        help="Skip Postgres; cache asset + embedding + caption data to .audit/offline/. Run `immy sync-offline <trip>` later to push. Requires one prior online run to have cached library info to ~/.immy/library.yml.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-scan trips even if `.audit/y_processed.yml` says they're done and nothing has changed. Useful after hand-editing files without bumping mtime.",
    ),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Phase Y.1/Y.2 — insert asset + asset_exif rows for every media file
    under one or more trip folders.

    Passing multiple folders is the right choice for overnight batch runs:
    MLX CLIP, InsightFace, and Whisper all load once for the entire batch
    instead of once per `immy process` invocation. Per-trip commit
    boundaries keep completed work durable even if a later trip fails or
    the user hits Ctrl-C.

    Requires `pg:` and `immich.library_id` in ~/.immy/config.yml.
    `--with-derivatives` (default) additionally requires `media:`.
    Idempotent via `checksum = sha1("path:" + originalPath)`.
    Drops `.audit/y_processed.yml` per trip so `immy promote` skips scan.
    """
    config = load_config(config_path)
    if config.immich is None:
        console.print(
            "[red]no immich: block in immy config[/red] — process needs "
            "`immich.library_id` to pick which library to write into."
        )
        raise typer.Exit(code=2)

    # Open pg connection once for the whole batch (online only).
    conn = None
    shared_library = None
    if not offline:
        if config.pg is None:
            console.print(
                "[red]no pg: block in immy config[/red] — add "
                "host/port/user/password/database to ~/.immy/config.yml, "
                "or run with --offline."
            )
            raise typer.Exit(code=2)
        try:
            conn = pg_mod.connect(config.pg)
        except Exception as e:
            console.print(
                f"[red]pg connect failed:[/red] {e}\n"
                f"[yellow]hint:[/yellow] if tailnet/NAS is unreachable, rerun "
                "with [bold]--offline[/bold] to cache work locally; sync later."
            )
            raise typer.Exit(code=2)
        _require_live_schema(conn)
        try:
            shared_library = pg_mod.fetch_library_info(conn, config.immich.library_id)
        except LookupError as e:
            console.print(f"[red]{e}[/red]")
            conn.close()
            raise typer.Exit(code=2)
        offline_mod.cache_library_info(shared_library)

    # Phase flags — identical across trips in the batch.
    compute = with_derivatives and config.media is not None
    if with_derivatives and config.media is None:
        console.print(
            "[yellow]note:[/yellow] `media:` block missing from config — "
            "skipping derivative generation. Add media.host_root + "
            "media.container_root to enable Y.2."
        )
    compute_clip = with_clip and compute
    if with_clip and not compute:
        console.print(
            "[yellow]note:[/yellow] --with-clip needs derivatives. Skipping CLIP."
        )
    compute_faces = with_faces and compute
    if with_faces and not compute:
        console.print(
            "[yellow]note:[/yellow] --with-faces needs derivatives. Skipping faces."
        )
    clip_model = (
        config.ml.clip_model
        if (config.ml is not None and config.ml.clip_model)
        else clip_mod.DEFAULT_MODEL
    )
    clip_backend = os.environ.get("IMMY_CLIP_BACKEND") or (
        config.ml.clip_backend if config.ml is not None else "mlx"
    )
    clip_endpoint = os.environ.get("IMMY_IMMICH_ML_URL") or (
        config.ml.immich_ml_url if config.ml is not None else None
    )
    allow_mlx_clip = allow_mlx_clip or bool(
        config.ml is not None and config.ml.allow_mlx_clip
    )
    if compute_clip:
        # Decide once, up front, so the marker below doesn't claim a CLIP
        # step this run won't perform. Other enrichers carry on.
        try:
            immich_clip = (
                None if conn is None else pg_mod.fetch_immich_clip_model(conn)
            )
            clip_block = process_mod.clip_guard_reason(
                clip_model=clip_model, clip_backend=clip_backend,
                allow_mlx_clip=allow_mlx_clip, immich_model=immich_clip,
            )
        except Exception as e:  # noqa: BLE001
            clip_block = f"could not read Immich's CLIP model ({e})"
        if clip_block:
            console.print(f"[yellow]CLIP skipped:[/yellow] {clip_block}")
            compute_clip = False
    transcript_model = transcripts_mod.DEFAULT_MODEL
    if config.ml is not None and config.ml.whisper_model:
        transcript_model = config.ml.whisper_model
    transcript_prompt = os.environ.get("IMMY_WHISPER_PROMPT") or (
        config.ml.whisper_prompt if config.ml is not None else None
    )
    transcript_backend = os.environ.get("IMMY_WHISPER_BACKEND") or (
        config.ml.whisper_backend if config.ml is not None else "mlx"
    )
    transcript_endpoint = os.environ.get("IMMY_WHISPER_ENDPOINT") or (
        config.ml.whisper_endpoint if config.ml is not None else None
    )
    captioner_config: captions_mod.CaptionerConfig | None = None
    if with_captions:
        ml = config.ml
        endpoint = (
            os.environ.get("IMMY_CAPTIONER_ENDPOINT")
            or (ml.captioner_endpoint if ml else None)
            or captions_mod.DEFAULT_ENDPOINT
        )
        explicit_model = (
            os.environ.get("IMMY_CAPTIONER_MODEL")
            or (ml.captioner_model if ml else None)
        )
        if explicit_model:
            model = explicit_model
        else:
            # No model pinned → ask LM Studio what's loaded right now and
            # use that; otherwise fall back to a known-installed VLM.
            model = (
                captions_mod.detect_lm_studio_model(endpoint)
                or captions_mod.LM_STUDIO_FALLBACK_MODEL
            )
        api_key_env = (
            os.environ.get("IMMY_CAPTIONER_API_KEY_ENV")
            or (ml.captioner_api_key_env if ml else None)
        )
        api_key = os.environ.get(api_key_env) if api_key_env else None
        prompt = (
            os.environ.get("IMMY_CAPTIONER_PROMPT")
            or (ml.captioner_prompt if ml else None)
            or captions_mod.DEFAULT_PROMPT
        )
        max_tokens = (
            int(os.environ["IMMY_CAPTIONER_MAX_TOKENS"])
            if os.environ.get("IMMY_CAPTIONER_MAX_TOKENS")
            else (
                ml.captioner_max_tokens
                if ml and ml.captioner_max_tokens
                else captions_mod.DEFAULT_MAX_TOKENS
            )
        )
        captioner_config = captions_mod.CaptionerConfig(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            prompt=prompt,
            max_tokens=max_tokens,
            extra_body=(ml.captioner_extra if ml else None),
        )
    phases: list[str] = []
    if compute:
        phases.append("derivatives")
    if compute_clip:
        phases.append("CLIP")
    if compute_faces:
        phases.append("faces")
    if with_transcripts:
        phases.append("transcripts")
    if with_captions:
        workers_note = f", {caption_workers}w" if caption_workers > 1 else ""
        phases.append(
            f"captions({captioner_config.model if captioner_config else '?'}{workers_note})"
        )
    if shared_library is not None:
        console.print(
            f"[bold]batch[/bold] {len(folders)} trip(s)\n"
            f"  library: {shared_library.id} owner={shared_library.owner_id}\n"
            f"  phases: {', '.join(phases) if phases else '[dim](EXIF + insert only)[/dim]'}"
        )
    else:
        console.print(
            f"[bold]batch[/bold] {len(folders)} trip(s)  [yellow](offline)[/yellow]\n"
            f"  phases: {', '.join(phases) if phases else '[dim](EXIF + insert only)[/dim]'}"
        )

    # What this run's markers record and what a cached marker must match.
    provenance = process_mod.marker_provenance(
        db=process_mod.marker_db_identity(config.pg, config.immich.library_id),
        offline=offline,
        steps=process_mod.marker_steps(
            compute_derivatives=compute,
            compute_clip=compute_clip,
            compute_faces=compute_faces,
            compute_transcripts=with_transcripts,
            compute_captions=with_captions,
            clip_model=clip_model,
            clip_backend=clip_backend,
            transcript_model=transcript_model,
            captioner_config=captioner_config,
        ),
    )

    ok = 0
    failed = 0
    interrupted = False
    try:
        for folder in folders:
            if offline:
                library, recovered = _resolve_offline_library(folder, config)
                if library is None:
                    console.print(
                        f"[red]{folder.name}: --offline needs library info[/red]; "
                        f"run `immy process` once online so library info gets cached. "
                        "Skipping."
                    )
                    failed += 1
                    continue
            else:
                library = shared_library
                recovered = False
            success = _run_one_trip(
                folder,
                library=library,
                conn=conn,
                offline=offline,
                recovered_from_marker=recovered,
                dry_run=dry_run,
                compute=compute,
                compute_clip=compute_clip,
                compute_faces=compute_faces,
                with_transcripts=with_transcripts,
                with_captions=with_captions,
                recaption=recaption,
                caption_fill_missing_only=captions_fill_missing,
                transcode_videos=transcode_videos,
                captioner_config=captioner_config,
                caption_workers=caption_workers,
                clip_model=clip_model,
                clip_backend=clip_backend,
                clip_endpoint=clip_endpoint,
                allow_mlx_clip=allow_mlx_clip,
                transcript_model=transcript_model,
                transcript_prompt=transcript_prompt,
                transcript_backend=transcript_backend,
                transcript_endpoint=transcript_endpoint,
                originals_root=config.originals_root,
                state_root=config.state_root,
                sidecars_root=config.sidecars_root,
                force=force,
                provenance=provenance,
            )
            if success:
                ok += 1
            else:
                failed += 1
    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[yellow]interrupted[/yellow] — stopping batch.")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if len(folders) > 1 or failed or interrupted:
        tag = "[yellow]partial[/yellow]" if interrupted else (
            "[green]done[/green]" if failed == 0 else "[yellow]done[/yellow]"
        )
        console.print(
            f"\n{tag} batch summary: {ok} ok"
            + (f", [red]{failed} failed[/red]" if failed else "")
            + (f", [yellow]{len(folders) - ok - failed} skipped (interrupted)[/yellow]"
               if interrupted else "")
        )
    if interrupted:
        raise typer.Exit(code=130)
    if failed:
        raise typer.Exit(code=1)


@app.command("sync-offline")
def sync_offline(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Replay `.audit/offline/*.yml` entries into Postgres.

    Intended flow: run `immy process --offline <trip>` on the Mac while
    the tailnet is down (CLIP / faces / captions / transcripts all compute
    locally), then once you're back on the tailnet run
    `immy sync-offline <trip>` to push the tiny SQL traffic. Each asset
    replays in its own transaction, so partial failures don't block the
    rest of the trip. Re-running is a no-op once every entry is marked
    synced — safe to script.
    """
    config = load_config(config_path)
    if config.pg is None:
        console.print(
            "[red]no pg: block in immy config[/red] — sync-offline needs the "
            "tailnet up and `pg:` set."
        )
        raise typer.Exit(code=2)

    # Same resolver `process --offline` used: NAS → state_root/<trip>/.audit
    # /offline; Mac (no roots) → `<trip>/.audit/offline`, unchanged.
    sync_paths = process_mod.resolve_writable_paths(
        folder,
        originals_root=config.originals_root,
        state_root=config.state_root,
        sidecars_root=config.sidecars_root,
    )
    offline_root = sync_paths.offline_dir
    entries = list(offline_mod.iter_entries(folder, offline_root=offline_root))
    if not entries:
        console.print(
            f"[dim]no offline entries under {offline_root}/"
            " — nothing to sync.[/dim]"
        )
        return

    pending = [e for _, e in entries if not e.get("synced")]
    console.print(
        f"[bold]sync-offline[/bold] {folder}\n"
        f"  {len(entries)} entry(ies) cached, [cyan]{len(pending)}[/cyan] pending"
    )
    if not pending:
        console.print("[green]✓[/green] all entries already synced.")
        return

    if config.immich is None:
        console.print(
            "[red]no immich: block in immy config[/red] — need library_id to "
            "resolve offline-placeholder owner/library values at sync time."
        )
        raise typer.Exit(code=2)
    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)
    _require_live_schema(conn)

    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
    except LookupError as e:
        console.print(f"[red]{e}[/red]")
        conn.close()
        raise typer.Exit(code=2)
    # Cache for future offline runs (if this is the first online contact
    # in a while, we want the cache warm).
    offline_mod.cache_library_info(library)

    def _progress(msg: str) -> None:
        console.print(msg, highlight=False, markup=False)

    try:
        summary = offline_mod.sync_trip(
            folder, conn, library=library, progress=_progress,
            offline_root=offline_root,
            journal_path=sync_paths.journal_path,
            marker_path=sync_paths.marker_path,
        )
    finally:
        if not conn.closed:
            conn.close()

    colour = "green" if summary["failed"] == 0 else "yellow"
    console.print(
        f"[{colour}]done.[/{colour}] "
        f"synced={summary['synced']}, skipped={summary['skipped']}, "
        f"failed={summary['failed']} of {summary['total']}"
    )
    if summary.get("clip_refused"):
        console.print(
            f"[yellow]CLIP vectors withheld for {summary['clip_refused']} "
            "asset(s)[/yellow] (model mismatch / mlx not allowed / no provenance); "
            "rows otherwise synced. Their CLIP journal entries were cleared — "
            "a normal `immy process` recomputes them."
        )
    if summary["failed"]:
        raise typer.Exit(code=1)


@app.command()
def similar(
    image: Path = typer.Argument(..., exists=True, dir_okay=False, help="Query photo (any JPEG/PNG/HEIC PIL can open)."),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
    limit: int = typer.Option(30, "--limit", "-n", help="How many neighbours to show."),
    min_sim: float = typer.Option(0.0, "--min-sim", help="Drop hits below this cosine similarity (0..1)."),
    no_videos: bool = typer.Option(False, "--no-videos", help="Images only."),
    backend: str = typer.Option("onnx", "--backend", help="onnx (Immich's own model, in-process) | immich-ml (NAS HTTP; needs ml.immich_ml_url). NOT mlx — different vector space."),
    as_json: bool = typer.Option(False, "--json", help="Print hits as JSON lines instead of a table."),
    by_face: bool = typer.Option(False, "--faces", help="Rank by face identity instead of whole frame: detect faces in IMAGE (Vision), ArcFace-embed, query face_search. One block per face."),
) -> None:
    """Find library assets that look like IMAGE — image-to-image search on Immich's CLIP index.

    Immich's UI only searches by text; this embeds the query photo with the
    same ViT-B-32 model Immich indexed with and asks pgvector for the nearest
    `smart_search` rows. Read-only. Similarity >= 0.95 is the same frame
    (a re-compressed copy scores ~0.99), 0.85-0.95 the same subject/pose
    (selfies of one person from different years all land 0.92-0.94), below
    that just the same kind of shot.
    Only assets Immich itself embedded are visible — immy-inserted ones never
    auto-queue SmartSearch, and coverage is printed so a miss is explainable.

    `--faces` switches to the face_search index (ArcFace, Mac-only: Vision
    detector + insightface). That ranks by WHO is in the shot — the person
    column tells you the identity — and only >= 0.93 means the same frame
    (a re-compressed copy scores ~0.95; other shots of the same person peak
    near 0.82).
    """
    import json as _json
    from . import clip as clip_mod

    config = load_config(config_path)
    if config.pg is None:
        console.print("[red]no pg: block in immy config[/red] — similar needs the tailnet up and `pg:` set.")
        raise typer.Exit(code=2)
    if backend == "mlx":
        console.print("[red]--backend mlx is not in Immich's vector space[/red]; use onnx or immich-ml.")
        raise typer.Exit(code=2)
    if by_face:
        _similar_by_face(config, image, limit=limit, min_sim=min_sim,
                         include_videos=not no_videos, as_json=as_json)
        return
    model = (config.ml.clip_model if config.ml and config.ml.clip_model else clip_mod.DEFAULT_MODEL)
    endpoint = config.ml.immich_ml_url if config.ml else None
    try:
        vec = clip_mod.embed(image, model_name=model, backend=backend, endpoint=endpoint)
    except (clip_mod.ClipUnavailable, clip_mod.ClipBackendError) as e:
        console.print(f"[red]embed failed:[/red] {e}")
        raise typer.Exit(code=2)
    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)
    with conn:
        embedded, live = similar_mod.coverage(conn)
        hits = similar_mod.search(
            conn, vec, limit=limit, include_videos=not no_videos, min_similarity=min_sim,
        )
    if as_json:
        for h in hits:
            print(_json.dumps({
                "assetId": h.asset_id, "similarity": round(h.similarity, 4), "label": h.label,
                "type": h.asset_type, "takenAt": h.taken_at.isoformat() if h.taken_at else None,
                "path": h.original_path, "city": h.city, "country": h.country,
            }))
        return
    console.print(
        f"[dim]{model} via {backend} · searchable {embedded:,} of {live:,} live assets "
        f"({embedded * 100 // max(live, 1)}%)[/dim]"
    )
    if not hits:
        console.print("no hits above the threshold")
        return
    t = Table(title=f"nearest to {image.name}")
    t.add_column("sim", justify="right")
    t.add_column("verdict")
    t.add_column("taken")
    t.add_column("where")
    t.add_column("file", no_wrap=True)
    for h in hits:
        style = "bold green" if h.label == "same frame" else ("yellow" if h.label == "same subject" else "")
        where = ", ".join(x for x in (h.city, h.country) if x)
        t.add_row(
            f"[{style}]{h.similarity:.3f}[/{style}]" if style else f"{h.similarity:.3f}",
            h.label,
            h.taken_at.strftime("%Y-%m-%d %H:%M") if h.taken_at else "",
            where,
            "/".join(h.original_path.rsplit("/", 3)[-3:])
            + (" [dim](video)[/dim]" if h.asset_type == "VIDEO" else ""),
        )
    console.print(t)
    console.print(f"[dim]open in Immich: {config.immich.url.rstrip('/') if config.immich else '<immich>'}/photos/<assetId> (use --json for ids)[/dim]")


def _similar_by_face(config, image: Path, *, limit: int, min_sim: float,
                     include_videos: bool, as_json: bool) -> None:
    import json as _json
    from collections import Counter
    from . import faces as faces_mod

    data = image.read_bytes()
    try:
        detected, w, h = faces_mod.detect(data)
        embedded = faces_mod.embed_faces(data, detected)
    except faces_mod.FacesUnavailable as e:
        console.print(f"[red]face pipeline unavailable:[/red] {e}")
        raise typer.Exit(code=2)
    if not embedded:
        console.print("no face detected in the query image")
        raise typer.Exit(code=1)
    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)
    with conn:
        for i, ef in enumerate(embedded, 1):
            hits = similar_mod.search_faces(
                conn, ef.embedding.tolist(), limit=limit,
                include_videos=include_videos, min_similarity=min_sim,
            )
            f = ef.face
            if as_json:
                for hh in hits:
                    print(_json.dumps({
                        "queryFace": i, "assetId": hh.asset_id, "similarity": round(hh.similarity, 4),
                        "label": hh.label, "person": hh.person, "type": hh.asset_type,
                        "takenAt": hh.taken_at.isoformat() if hh.taken_at else None, "path": hh.original_path,
                    }))
                continue
            who = Counter(hh.person or "(unnamed)" for hh in hits).most_common(3)
            console.print(
                f"[dim]face {i}/{len(embedded)} bbox ({f.x1},{f.y1})-({f.x2},{f.y2}) in {w}x{h} · "
                f"top-{len(hits)} identity: {', '.join(f'{n} x{c}' for n, c in who)}[/dim]"
            )
            t = Table(title=f"faces nearest to {image.name} (face {i})")
            t.add_column("sim", justify="right"); t.add_column("verdict"); t.add_column("person")
            t.add_column("taken"); t.add_column("file", no_wrap=True)
            for hh in hits:
                style = "bold green" if hh.label == "same frame" else ""
                t.add_row(
                    f"[{style}]{hh.similarity:.3f}[/{style}]" if style else f"{hh.similarity:.3f}",
                    hh.label, hh.person or "",
                    hh.taken_at.strftime("%Y-%m-%d %H:%M") if hh.taken_at else "",
                    "/".join(hh.original_path.rsplit("/", 3)[-3:])
                    + (" [dim](video)[/dim]" if hh.asset_type == "VIDEO" else ""),
                )
            console.print(t)


@app.command("db-setup")
def db_setup(
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL we'd run; make no changes."),
) -> None:
    """Create immy-owned indexes on the Immich DB (idempotent, safe to re-run).

    Immich 2.7 indexes filenames and place names with trigram GIN for
    fuzzy search, but `asset_exif.description` — where `immy` writes
    Whisper transcript excerpts and VLM captions — has no index. At a
    few thousand assets a sequential scan is fine; past ~50 k it starts
    hurting search latency in the UI.

    This command adds `immy_idx_asset_exif_description_trigram`, a GIN
    trigram index matching the pattern Immich uses for its own text
    columns (`f_unaccent(description) gin_trgm_ops`). `IF NOT EXISTS`
    guards re-runs, and the `immy_` prefix keeps us out of Immich's
    migration namespace so a future server upgrade can add a similarly-
    named index without colliding.
    """
    config = load_config(config_path)
    if config.pg is None:
        console.print(
            "[red]no pg: block in immy config[/red] — db-setup needs the "
            "tailnet up and `pg:` set."
        )
        raise typer.Exit(code=2)
    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)

    # Matching Immich's own pattern exactly: `f_unaccent(col) gin_trgm_ops`.
    # The `f_unaccent` wrapper is Immich's migration artefact — plain
    # `unaccent()` isn't IMMUTABLE and can't back an index. We reuse it
    # instead of creating a second helper.
    stmts = [
        (
            "immy_idx_asset_exif_description_trigram",
            """CREATE INDEX IF NOT EXISTS
               "immy_idx_asset_exif_description_trigram"
               ON asset_exif
               USING gin (f_unaccent(description) gin_trgm_ops)""",
        ),
    ]
    console.print(f"[bold]db-setup[/bold] {config.pg.host}:{config.pg.port}/{config.pg.database}")
    for name, sql in stmts:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_indexes WHERE indexname = %s", (name,),
            )
            exists = cur.fetchone() is not None
        if exists:
            console.print(f"  [dim]✓ {name} already present[/dim]")
            continue
        if dry_run:
            console.print(f"  [yellow]would create[/yellow] {name}")
            console.print(f"    [dim]{' '.join(sql.split())}[/dim]")
            continue
        console.print(f"  [yellow]creating[/yellow] {name} (may take a moment on large libraries)…")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            console.print(f"  [green]✓[/green] {name}")
        except Exception as e:
            conn.rollback()
            console.print(f"  [red]failed:[/red] {e}")
    conn.close()


@app.command("cluster")
def cluster(
    dry_run: bool = typer.Option(
        True, "--dry-run/--apply",
        help="Default: print proposed albums only. `--apply` creates/updates "
             "Immich albums via the API.",
    ),
    min_assets: int = typer.Option(
        clustering_mod.DEFAULT_MIN_ASSETS, "--min-assets",
        help=f"Drop clusters smaller than this (default: {clustering_mod.DEFAULT_MIN_ASSETS}).",
    ),
    max_gap_hours: float = typer.Option(
        clustering_mod.DEFAULT_MAX_GAP_HOURS, "--max-gap-hours",
        help=f"Time gap that splits an event (default: {clustering_mod.DEFAULT_MAX_GAP_HOURS} h).",
    ),
    max_km: float = typer.Option(
        clustering_mod.DEFAULT_MAX_KM, "--max-km",
        help=f"Distance from centroid that splits an event (default: {clustering_mod.DEFAULT_MAX_KM} km).",
    ),
    prune: bool = typer.Option(
        False, "--prune/--no-prune",
        help="Also remove assets immy put in an album on an earlier run that "
             "are no longer in that event. Never touches assets immy did not "
             "add (tracked in cluster-ledger.json under state_root or ~/.immy).",
    ),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Group assets by (time, lat, lon) into events and auto-create albums.

    Pulls every asset with `dateTimeOriginal` + `latitude` + `longitude`
    from `asset_exif`, runs a sweep-based cluster (new event when time
    gap > `--max-gap-hours` OR distance > `--max-km`), names each event
    from the dominant city/country Immich's own reverse-geocode worker
    already wrote, and (with `--apply`) creates or updates one album per
    event.

    Idempotent via a `immy-cluster:<stable_key>` marker line embedded in
    each album's description. The key is derived from rounded centroid +
    start date so late-arriving photos don't spawn duplicate albums.
    Without `--prune` assets are only ever added — if an asset's event
    membership changes across runs, it ends up in both albums. `--prune`
    removes the stale copies, limited to assets immy itself added (a
    ledger records them), so hand-added photos always stay.
    """
    config = load_config(config_path)
    if config.pg is None:
        console.print("[red]no pg: block in immy config[/red]")
        raise typer.Exit(code=2)
    if config.immich is None:
        console.print("[red]no immich: block in immy config[/red] — "
                      "cluster --apply needs api_key to create albums.")
        raise typer.Exit(code=2)

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)

    # Pull time+gps rows. Soft-deleted assets are filtered server-side —
    # otherwise a recent delete would resurrect as a cluster member on
    # the next run and re-PUT into the album via the idempotent endpoint.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ae."assetId", ae."dateTimeOriginal",
                   ae.latitude, ae.longitude, ae.city, ae.country
            FROM asset_exif ae
            JOIN asset a ON a.id = ae."assetId"
            WHERE ae."dateTimeOriginal" IS NOT NULL
              AND ae.latitude IS NOT NULL
              AND ae.longitude IS NOT NULL
              AND a."deletedAt" IS NULL
        """)
        rows = cur.fetchall()
    conn.close()

    points = [
        clustering_mod.AssetPoint(
            asset_id=str(r[0]), when=r[1],
            lat=float(r[2]), lon=float(r[3]),
            city=r[4], country=r[5],
        )
        for r in rows
    ]
    clusters = clustering_mod.cluster_assets(
        points,
        max_gap_hours=max_gap_hours,
        max_km=max_km,
        min_assets=min_assets,
    )
    total_assets = sum(len(c.assets) for c in clusters)
    console.print(
        f"[bold]cluster[/bold] — {len(points)} geo-dated assets → "
        f"{len(clusters)} event(s) of ≥{min_assets} "
        f"({total_assets} asset(s) in events, "
        f"{len(points) - total_assets} ungrouped)"
    )
    if not clusters:
        return

    for c in clusters:
        console.print(
            f"  [cyan]{c.name()}[/cyan]  "
            f"[dim]{len(c.assets)} asset(s), key={c.stable_key()}[/dim]"
        )

    ledger_path = (
        (config.state_root or Path.home() / ".immy") / clustering_mod.LEDGER_FILENAME
    )
    ledger = clustering_mod.load_ledger(ledger_path)
    plan = clustering_mod.prune_plan(ledger, clusters) if prune else {}
    if plan:
        console.print(
            f"  [yellow]prune[/yellow] {sum(len(v) for v in plan.values())} stale "
            f"asset-link(s) across {len(plan)} album(s)"
        )

    if dry_run:
        console.print(
            "\n[yellow]dry-run[/yellow] — pass `--apply` to create/update "
            f"{len(clusters)} album(s) in Immich."
        )
        return

    # Apply phase. Fetch every album once (small N in practice); build a
    # key→album map from descriptions, then per cluster either update
    # the matching album or create a fresh one.
    client = ImmichClient(
        url=config.immich.url,
        api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    )
    key_to_album: dict[str, dict] = {}
    existing = client._request("GET", "/api/albums")
    if isinstance(existing, list):
        for alb in existing:
            if not isinstance(alb, dict):
                continue
            k = clustering_mod.extract_cluster_key(alb.get("description"))
            if k:
                key_to_album[k] = alb

    created = 0
    updated = 0
    added_assets_total = 0
    for c in clusters:
        key = c.stable_key()
        name = c.name()
        marker = clustering_mod.cluster_marker_line(key)
        # Description: name as first line (human-visible), marker on
        # second line (machine-parseable, ignored by users).
        description = f"{name}\n{marker}"
        asset_ids = [a.asset_id for a in c.assets]
        existing_alb = key_to_album.get(key)
        if existing_alb is None:
            album_id = client.create_album(
                name, description=description, asset_ids=asset_ids,
            )
            if album_id:
                created += 1
                added_assets_total += len(asset_ids)
                ledger[key] = sorted(set(ledger.get(key, [])) | set(asset_ids))
                clustering_mod.save_ledger(ledger_path, ledger)
                console.print(
                    f"  [green]created[/green] {name} "
                    f"[dim]({len(asset_ids)} asset(s))[/dim]"
                )
            else:
                console.print(f"  [red]create failed[/red] {name}")
        else:
            album_id = existing_alb["id"]
            # Don't touch name/description if they match — patch is a
            # no-op but avoids an unnecessary updatedAt bump.
            if existing_alb.get("description") != description:
                client.update_album(album_id, description=description)
            result = client.add_assets_to_album(album_id, asset_ids)
            added = sum(1 for r in result if isinstance(r, dict) and r.get("success"))
            updated += 1
            added_assets_total += added
            # Union, not replace: without --prune the stale claims must be
            # remembered so a later --prune can still find them.
            ledger[key] = sorted(set(ledger.get(key, [])) | set(asset_ids))
            clustering_mod.save_ledger(ledger_path, ledger)
            console.print(
                f"  [green]updated[/green] {name} "
                f"[dim]({added} new, {len(asset_ids) - added} already present)[/dim]"
            )

    removed_total = 0
    current_keys = {c.stable_key() for c in clusters}
    for key, stale in plan.items():
        album = key_to_album.get(key)
        if album is not None:
            result = client.remove_assets_from_album(album["id"], stale)
            removed = sum(1 for r in result if isinstance(r, dict) and r.get("success"))
            removed_total += removed
            console.print(
                f"  [yellow]pruned[/yellow] {album.get('albumName') or key} "
                f"[dim]({removed} removed, {len(stale) - removed} already gone)[/dim]"
            )
        # The album was deleted by hand, or the links are now removed: either
        # way immy no longer claims these assets for this key.
        keep = set(ledger.get(key, [])) - set(stale)
        if keep or key in current_keys:
            ledger[key] = sorted(keep)
        else:
            ledger.pop(key, None)
        clustering_mod.save_ledger(ledger_path, ledger)

    console.print(
        f"\n[green]✓[/green] {created} album(s) created, "
        f"{updated} updated, {added_assets_total} asset-link(s) added"
        + (f", {removed_total} pruned" if prune else "")
    )


@app.command("trips")
def trips(
    dry_run: bool = typer.Option(
        True, "--dry-run/--apply",
        help="Default: print the proposed trips only. `--apply` creates/updates "
             "one Immich album per trip.",
    ),
    since: str = typer.Option(None, "--since", help="Only trips starting on/after YYYY-MM-DD."),
    until: str = typer.Option(None, "--until", help="Only trips starting on/before YYYY-MM-DD."),
    min_assets: int = typer.Option(
        None, "--min-assets",
        help=f"Trips with fewer assets get no album (default: config, else {trips_mod.DEFAULT_MIN_ASSETS}).",
    ),
    max_gap_days: int = typer.Option(
        None, "--max-gap-days",
        help=f"Days with nothing geotagged a trip may bridge (default: config, else {trips_mod.DEFAULT_MAX_GAP_DAYS}).",
    ),
    transit_days: int = typer.Option(
        None, "--transit-days",
        help="A run this short that touches another folds into it as a stopover "
             f"(default: config, else {trips_mod.DEFAULT_TRANSIT_DAYS}; 0 disables).",
    ),
    owner: str = typer.Option(
        None, "--owner",
        help="Immich user email whose assets to use. Required when the server has more than one user.",
    ),
    tags: bool = typer.Option(
        False, "--tags/--no-tags",
        help="Also tag each trip's assets `<tag_root>/<year>/<album name>`: the "
             "nesting albums can't do.",
    ),
    prune: bool = typer.Option(
        False, "--prune/--no-prune",
        help="Remove album links and trip tags immy added earlier that no longer "
             "belong (an asset moved to another trip, a trip that disappeared). "
             "Never touches assets or tags immy did not add; albums are kept.",
    ),
    refresh_descriptions: bool = typer.Option(
        False, "--refresh-descriptions",
        help="Rewrite each album's generated description (dates + itinerary) even "
             "if it was edited or predates tracking. Your own lines are kept only "
             "if you wrote them above the marker; without this flag only unedited "
             "descriptions follow the trip.",
    ),
    csv_path: Path = typer.Option(None, "--csv", help="Also write the trip table here, for review."),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Find trips in the library's day-by-day geography; one album each.

    Each day gets a country by majority vote of its geotagged assets. Days
    at a configured home (`trips.homes`) are not travel; the rest are cut
    into trips where the region changes (Oceania, Southeast Asia, …; most
    European countries count alone) or the track goes quiet for more than
    `--max-gap-days`. A trip's album holds every asset dated inside it,
    with or without GPS. See docs/TRIPS.md.

    Idempotent via an `immy-trip:<key>` line in each album description;
    names and description text you edit in Immich are kept.
    """
    import csv
    from datetime import date as _date

    config = load_config(config_path)
    if config.pg is None:
        console.print("[red]no pg: block in immy config[/red]")
        raise typer.Exit(code=2)
    if not dry_run and config.immich is None:
        console.print("[red]no immich: block in immy config[/red] — "
                      "trips --apply needs api_key to create albums.")
        raise typer.Exit(code=2)
    tc = config.trips or config_mod.TripsConfig()
    min_assets = min_assets if min_assets is not None else (
        tc.min_assets if tc.min_assets is not None else trips_mod.DEFAULT_MIN_ASSETS)
    max_gap_days = max_gap_days if max_gap_days is not None else (
        tc.max_gap_days if tc.max_gap_days is not None else trips_mod.DEFAULT_MAX_GAP_DAYS)
    transit_days = transit_days if transit_days is not None else (
        tc.transit_days if tc.transit_days is not None else trips_mod.DEFAULT_TRANSIT_DAYS)
    tag_root = tc.tag_root or trips_mod.DEFAULT_TAG_ROOT
    placeholder_min = (tc.placeholder_min if tc.placeholder_min is not None
                       else trips_mod.DEFAULT_PLACEHOLDER_MIN)
    try:
        since_d = _date.fromisoformat(since) if since else None
        until_d = _date.fromisoformat(until) if until else None
    except ValueError as e:
        console.print(f"[red]bad date:[/red] {e}")
        raise typer.Exit(code=2)

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)

    # `localDateTime` is the shot's wall-clock time stored as if it were UTC,
    # so reading it back in UTC yields the local calendar day.
    with conn.cursor() as cur:
        cur.execute('SELECT id, email FROM "user" WHERE "deletedAt" IS NULL')
        users = cur.fetchall()
        if owner:
            match = [u for u in users if u[1] == owner]
            if not match:
                console.print(f"[red]no Immich user {owner!r}[/red]")
                conn.close()
                raise typer.Exit(code=2)
            owner_id = str(match[0][0])
        elif len(users) == 1:
            owner_id = str(users[0][0])
        else:
            console.print(f"[red]{len(users)} Immich users[/red] — pass --owner <email>.")
            conn.close()
            raise typer.Exit(code=2)
        params = {"owner": owner_id, "placeholder_min": placeholder_min}
        cur.execute(trips_mod.DAY_BUCKETS_SQL, params)
        buckets = [
            trips_mod.PlaceCount(day=r[0], country=r[1], city=r[2],
                                 lat=float(r[3]), lon=float(r[4]), n=int(r[5]))
            for r in cur.fetchall()
        ]
        cur.execute(trips_mod.ASSETS_SQL, params)
        rows = cur.fetchall()
        assets = [(str(r[0]), r[1]) for r in rows if not r[2]]
        placeholders = len(rows) - len(assets)
    conn.close()

    days = trips_mod.build_days(buckets)
    found = trips_mod.segment(
        days,
        homes=list(tc.homes),
        regions=trips_mod.Regions(tc.regions),
        max_gap_days=max_gap_days,
        transit_days=transit_days,
    )
    trips_mod.assign_assets(found, assets)
    asset_day = dict(assets)
    kept = [t for t in found if trips_mod.keep(t, min_assets=min_assets)]
    small = len(found) - len(kept)
    if since_d:
        kept = [t for t in kept if t.start >= since_d]
    if until_d:
        kept = [t for t in kept if t.start <= until_d]

    home_days = sum(1 for d in days if any(h.matches(d) for h in tc.homes))
    console.print(
        f"[bold]trips[/bold] — {len(days)} geotagged day(s), {home_days} at home, "
        f"{len(assets)} timeline asset(s)"
        + (f" (+{placeholders} with a placeholder date, skipped)" if placeholders else "")
        + f" → {len(found)} trip(s), "
        f"{small} under {min_assets} assets skipped"
        + (f", {len(kept)} in range" if since_d or until_d else "")
    )
    table = Table(show_lines=False, pad_edge=False)
    for col, kw in (("album", {}), ("dates", {}), ("days", {"justify": "right"}),
                    ("assets", {"justify": "right"}), ("countries", {})):
        table.add_column(col, **kw)
    for t in kept:
        table.add_row(
            t.name(), trips_mod.format_range(t.start, t.end), str(t.span_days),
            str(len(t.asset_ids)), ", ".join(n for _, n in t.countries()),
        )
        legs = t.legs()
        if len(legs) > 1:
            for leg in legs:
                table.add_row(f"[dim]  {leg.short_label()}[/dim]", "", "", "", "")
    if kept:
        console.print(table)
    by_year: dict[int, int] = {}
    for t in kept:
        by_year[t.start.year] = by_year.get(t.start.year, 0) + 1
    if by_year:
        console.print("per year: " + ", ".join(f"{y} {n}" for y, n in sorted(by_year.items())))

    if csv_path:
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["key", "album", "start", "end", "days", "geotagged_days",
                        "assets", "region", "countries", "legs", "tag"])
            for t in kept:
                w.writerow([
                    t.key(), t.name(), t.start.isoformat(), t.end.isoformat(),
                    t.span_days, len(t.days), len(t.asset_ids), t.region_label or "",
                    "; ".join(n for _, n in t.countries()),
                    "; ".join(leg.short_label() for leg in t.legs()),
                    trips_mod.tag_for(t, tag_root),
                ])
        console.print(f"wrote {csv_path}")

    ledger_path = (config.state_root or Path.home() / ".immy") / trips_mod.LEDGER_FILENAME
    ledger = trips_mod.load_ledger(ledger_path)

    def in_scope(start):
        return (not since_d or start >= since_d) and (not until_d or start <= until_d)

    if dry_run:
        _, orphans = trips_mod.match_ledger(kept, ledger, in_scope=in_scope)
        if orphans:
            console.print(
                f"[yellow]{len(orphans)} earlier trip(s) no longer found[/yellow] "
                "(home added, merged, or under min_assets): `--apply --prune` removes "
                "what immy put in their albums and tags; the albums stay."
            )
        console.print(
            f"\n[yellow]dry-run[/yellow] — pass `--apply` to create/update {len(kept)} album(s)"
            + (" and tag their assets" if tags else "") + "."
        )
        return

    client = ImmichClient(
        url=config.immich.url,
        api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    )
    key_to_album: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    existing = client._request("GET", "/api/albums")
    for alb in existing if isinstance(existing, list) else []:
        if isinstance(alb, dict):
            by_id[str(alb.get("id"))] = alb
            k = trips_mod.extract_key(alb.get("description"))
            if k:
                key_to_album[k] = alb

    def album_for(key, entry):
        """The album a ledger key lives in: by its marker line, else by the
        album id the ledger recorded (the marker may have been edited)."""
        if key and key in key_to_album:
            return key_to_album[key]
        return by_id.get(str((entry or {}).get("album_id")))

    def ok_ids(result) -> set[str]:
        return {str(r.get("id")) for r in result if isinstance(r, dict) and r.get("success")}

    tag_conn = None

    def tconn():
        nonlocal tag_conn
        if tag_conn is None:
            tag_conn = pg_mod.connect(config.pg)
        return tag_conn

    pairs, orphans = trips_mod.match_ledger(
        kept, ledger, album_keys=set(key_to_album), in_scope=in_scope)
    created = updated = linked = removed_total = tagged = untagged = 0
    for t, old_key in pairs:
        key = t.key()
        ids = t.asset_ids
        entry = ledger.get(old_key) if old_key else None
        album = album_for(old_key, entry) if old_key else None
        # Claims carry over only while the album they were made in exists;
        # a hand-deleted album starts from scratch. Tag ownership doesn't
        # depend on the album.
        previous = set((entry or {}).get("assets", [])) if album is not None else set()
        if album is None:
            album_id = client.create_album(
                t.name(), description=trips_mod.description_for(t), asset_ids=ids,
            )
            if not album_id:
                console.print(f"  [red]create failed[/red] {t.name()}")
                continue
            owned = set(ids)  # a fresh album holds only what immy put in it
            created += 1
            linked += len(ids)
            console.print(f"  [green]created[/green] {t.name()} [dim]({len(ids)} asset(s))[/dim]")
        else:
            album_id = album["id"]
            desc = album.get("description") or ""
            generated = trips_mod.description_for(t)
            if desc == (entry or {}).get("description") or (
                    refresh_descriptions and desc != generated):
                # Unedited since immy wrote it (or forced): follow the trip.
                # Forced keeps any lines the user put above immy's block.
                own = [ln for ln in desc.splitlines()
                       if ln.strip() and not trips_mod.is_generated_line(ln)]
                new_desc = "\n".join(own + [generated])
                if new_desc != desc:
                    client.update_album(album_id, description=new_desc)
                    desc = new_desc
            elif trips_mod.extract_key(desc) != key:
                # Keep whatever the user wrote; only the marker line moves.
                kept_lines = [ln for ln in desc.splitlines()
                              if not ln.strip().startswith(trips_mod.IMMY_TRIP_MARKER)]
                desc = "\n".join(kept_lines + [trips_mod.marker_line(key)])
                client.update_album(album_id, description=desc)
            added = ok_ids(client.add_assets_to_album(album_id, ids))
            linked += len(added)
            updated += 1
            # Owned: what immy owned before and still wants, plus what it just
            # added. An asset that was already there (added by hand) and not
            # owned before stays the user's.
            owned = (previous & set(ids)) | added
            stale = sorted(previous - set(ids))
            if prune and stale:
                client.remove_assets_from_album(album_id, stale)
                removed_total += len(stale)
            elif stale:
                owned |= set(stale)  # remembered so a later --prune can remove them
            console.print(
                f"  [green]updated[/green] {album.get('albumName') or t.name()} "
                f"[dim]({len(added)} new" + (f", {len(stale)} pruned" if prune and stale else "") + ")[/dim]"
            )

        owned_tags = trips_mod.owned_pairs(entry)
        if tags and entry is not None and "tags" not in entry:
            owned_tags = trips_mod.backfill_owned_tags(
                tconn(), owner_id, list(entry.get("assets", [])), tag_root)
        if tags:
            # Most specific level only (the leg); Immich lists a parent
            # tag's assets through its closure table. Linked by SQL with the
            # tag list locked: see trips.link_tags for why not the tag API.
            leg_pairs = trips_mod.leg_tags(t, tag_root)
            tag_ids = client.upsert_tags([name for _, name in leg_pairs])
            split = dict(trips_mod.assets_by_leg(t, asset_day))
            links = []
            for leg, name in leg_pairs:
                tag_id = tag_ids.get(name)
                if not tag_id:
                    console.print(f"  [red]tag upsert failed[/red] {name}")
                    continue
                links += [(aid, tag_id, name) for aid in split.get(leg, [])]
            if links:
                trips_mod.link_tags(tconn(), links)
                tagged += len(links)
            wanted = {(a, v) for a, _, v in links}
            stale_tags = owned_tags - wanted
            if prune and stale_tags:
                trips_mod.unlink_tags(tconn(), owner_id, sorted(stale_tags))
                untagged += len(stale_tags)
                stale_tags = set()
            owned_tags = wanted | stale_tags

        if old_key and old_key != key:
            ledger.pop(old_key, None)
        ledger[key] = {
            "start": t.start.isoformat(), "end": t.end.isoformat(),
            "region": t.region, "album_id": str(album_id),
            "assets": sorted(owned), "tags": trips_mod.tags_by_value(owned_tags),
            # What immy last wrote, if the album still shows exactly that;
            # an edited description is never tracked (so never overwritten).
            "description": (trips_mod.description_for(t) if album is None
                            else (desc if desc == trips_mod.description_for(t) else
                                  (entry or {}).get("description"))),
        }
        trips_mod.save_ledger(ledger_path, ledger)

    # Trips that no longer exist: take back only what immy put there. The
    # album itself stays, since it may hold the user's own additions and edits.
    if orphans and not prune:
        console.print(
            f"[yellow]{len(orphans)} earlier trip(s) no longer found[/yellow] — "
            "re-run with `--prune` to remove what immy put in their albums and tags."
        )
    for key in orphans if prune else []:
        entry = ledger.get(key) or {}
        album = album_for(key, entry)
        claimed = sorted(entry.get("assets", []))
        if album is not None and claimed:
            client.remove_assets_from_album(album["id"], claimed)
            removed_total += len(claimed)
        stale_tags = sorted(trips_mod.owned_pairs(entry))
        if stale_tags:
            trips_mod.unlink_tags(tconn(), owner_id, stale_tags)
            untagged += len(stale_tags)
        console.print(
            f"  [yellow]retired[/yellow] {(album or {}).get('albumName') or key} "
            f"[dim]({len(claimed)} album link(s), {len(stale_tags)} tag(s) removed; album kept)[/dim]"
        )
        ledger.pop(key, None)
        trips_mod.save_ledger(ledger_path, ledger)

    if tag_conn is not None:
        tag_conn.close()
    console.print(
        f"\n[green]✓[/green] {created} album(s) created, {updated} updated, "
        f"{linked} asset-link(s) added" + (f", {removed_total} pruned" if prune else "")
        + (f", {tagged} asset(s) tagged (locked)" if tags else "")
        + (f", {untagged} stale tag(s) removed" if untagged else "")
    )


@app.command("repair-thumbs")
def repair_thumbs(
    folders: list[Path] = typer.Argument(..., help="Trip folder(s) whose Immich thumbnails are broken."),
    parallel: int = typer.Option(8, "-P", "--parallel", help="Concurrent derivative generations (default 8)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would regenerate; touch nothing."),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Repair broken thumbnails IN PLACE for assets already in Immich.

    For assets scanned while their originals were offline (Immich wrote a
    `__offline_placeholder__` thumbnail, or none), regenerate thumbnail +
    preview locally on the laptop and upsert the `asset_file` rows — reusing
    each asset's existing UUID, so albums/favorites/faces survive. No delete,
    no re-ingest, no NAS thumbnail CPU. Idempotent; resumable (re-run safely).
    """
    from . import repair as repair_mod

    config = load_config(config_path)
    if config.pg is None or config.immich is None or config.media is None:
        console.print("[red]repair-thumbs needs pg + immich + media blocks in config.[/red]")
        raise typer.Exit(code=2)

    totals = {"broken": 0, "generated": 0, "rows": 0, "no_src": 0, "failed": 0}
    for folder in folders:
        if not folder.is_dir():
            totals["failed"] += 1
            console.print(f"[red]not a folder:[/red] {folder}")
            continue
        console.print(f"[bold]repair-thumbs[/bold] {folder.name} …")
        try:
            res = repair_mod.repair_trip(
                folder, config, parallel=parallel, dry_run=dry_run,
                progress=lambda done, total: None,
            )
        except Exception as e:  # noqa: BLE001 — one trip's DB/IO failure must not stop the rest
            res = repair_mod.TripRepair(trip=folder.name, status="error", detail=str(e))
        totals["broken"] += res.broken
        totals["generated"] += res.generated
        totals["rows"] += res.rows_upserted
        totals["no_src"] += res.missing_source
        if res.status == "error":
            totals["failed"] += 1
            console.print(f"  [red]error:[/red] {res.detail}")
        elif res.broken == 0:
            console.print("  [green]clean[/green] — no broken thumbnails")
        else:
            tail = f"  ([yellow]{res.missing_source} no local source[/yellow])" if res.missing_source else ""
            verb = "would regenerate" if dry_run else "regenerated"
            console.print(
                f"  [green]{verb}[/green] {res.generated}/{res.broken}"
                + (f", {res.rows_upserted} asset_file row(s) upserted" if not dry_run else "")
                + tail
            )
    console.print(
        f"\n[bold]Done.[/bold] {totals['broken']} broken, {totals['generated']} "
        f"{'would regenerate' if dry_run else 'regenerated'}, "
        f"{totals['rows']} row(s) upserted, {totals['no_src']} no source, "
        f"[red]{totals['failed']} trip(s) failed[/red]"
    )
    if totals["failed"]:
        raise typer.Exit(code=1)


@app.command("snapshot")
def snapshot(
    out: Path = typer.Option(
        Path.home() / ".immy" / "library-snapshot.sqlite", "--out",
        help="Where to write the SQLite snapshot.",
    ),
    library_id: str = typer.Option(
        None, "--library",
        help="Restrict snapshot to a single library UUID (default: all).",
    ),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config."),
) -> None:
    """Dump the Immich library index into a portable SQLite file.

    One row per asset: `(asset_id, filename, size, checksum, taken_at,
    type, library_id)`. Ships everything needed to answer "is this file
    already in Immich?" from any other machine, with no network access.

    Foundation for `immy find-duplicates` and (eventually) the Apple Photos
    importer. Read-only on Immich — safe to run anytime, including against
    a production DB during a scan.
    """
    config = load_config(config_path)
    if config.pg is None:
        console.print("[red]no pg: block in immy config[/red]")
        raise typer.Exit(code=2)

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)

    console.print(
        f"[bold]snapshot[/bold] {config.pg.host}:{config.pg.port}/{config.pg.database}"
        f" → [cyan]{out}[/cyan]"
    )
    # Build beside the target and os.replace at the end: the previous snapshot
    # survives a failed/interrupted run instead of being unlinked up front.
    tmp_out = snapshot_mod.temp_path(out)
    db = snapshot_mod.create(tmp_out)
    try:
        count = snapshot_mod.write_rows(
            db, snapshot_mod.fetch_rows(conn, library_id),
        )
        # v2: marker (immy-cluster) albums so `immy match` can rebuild trips
        # offline. Only albums carrying an `immy-cluster:` marker are kept.
        albums = snapshot_mod.fetch_albums(conn)
        album_ids = {a.album_id for a in albums}
        membership = snapshot_mod.fetch_album_assets(conn, album_ids)
        album_count = snapshot_mod.write_albums(db, albums, membership)
        snapshot_mod.write_meta(
            db,
            server_host=f"{config.pg.host}:{config.pg.port}",
            library_id=library_id,
            asset_count=count,
        )
        db.close()
        snapshot_mod.publish(tmp_out, out)
    except BaseException:
        db.close()
        tmp_out.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    size_mb = out.stat().st_size / (1024 * 1024)
    console.print(
        f"  [green]✓[/green] {count:,} asset(s), {album_count:,} album(s)"
        f" → {size_mb:.1f} MB at [cyan]{out}[/cyan]"
    )


def _open_snapshot_or_exit(path: Path):
    """open_for_read, but an incomplete snapshot is a clean exit-2 error."""
    try:
        return snapshot_mod.open_for_read(path)
    except snapshot_mod.IncompleteSnapshotError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)


@app.command("backfill-dates")
def backfill_dates(
    folders: list[Path] = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Trip folder(s) whose local files still carry the date source "
             "(DJI .SRT sidecars, filename stamps).",
    ),
    apply: bool = typer.Option(
        False, "--apply",
        help="Write the dates. Default is a dry-run report only.",
    ),
    timezone: str = typer.Option(
        None, "--timezone",
        help="Force an IANA zone (e.g. Indian/Mauritius) for the whole run. "
             "Default: per-clip from its own SRT GPS, else notes / EXIF-GPS / "
             "trip SRT-GPS; if none, wall numbers as UTC (order ok, offset).",
    ),
    retime: bool = typer.Option(
        False, "--retime",
        help="Also re-date assets that ALREADY have a date — to correct a "
             "wrong earlier write (e.g. a mixed-location folder zoned wrong). "
             "Overwrites existing dateTimeOriginal/localDateTime/timeZone.",
    ),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config."),
) -> None:
    """Backfill capture dates for already-ingested assets that landed dateless.

    For DJI footage promoted before the SRT-date rule existed, the asset_exif
    row exists with a NULL dateTimeOriginal — and `immy process` can't fix it
    (`ON CONFLICT DO NOTHING`). This reads each file's `.SRT` (or embedded /
    filename date), matches it to the Immich asset by originalPath, and
    UPDATEs `asset_exif.dateTimeOriginal` + `asset.localDateTime` (what the
    timeline sorts by) — only ever touching rows that are still dateless.

    Read-only by default; pass --apply to write. After applying, re-run
    `immy cluster` if you use auto-albums (it selects on dateTimeOriginal).
    """
    config = load_config(config_path)
    if config.pg is None or config.immich is None:
        console.print("[red]need pg: + immich.library_id in config[/red]")
        raise typer.Exit(code=2)
    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)
    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
    except LookupError as e:
        console.print(f"[red]{e}[/red]")
        conn.close()
        raise typer.Exit(code=2)

    mode = "[yellow]DRY-RUN[/yellow]" if not apply else "[red]APPLY[/red]"
    grand_written = grand_cands = 0
    try:
        for folder in folders:
            # Sidecars (rule fixes / user corrections) live under
            # sidecars_root on the NAS — read them where they are, or
            # --retime would overwrite a correction with the embedded value.
            plan = backfill_dates_mod.plan_folder(
                conn, library, folder, tz_override=timezone, retime=retime,
                paths=process_mod.resolve_writable_paths(
                    folder,
                    originals_root=config.originals_root,
                    state_root=config.state_root,
                    sidecars_root=config.sidecars_root,
                ),
            )
            tz_disp = plan.tz_name or "(none — wall-as-UTC)"
            console.print(
                f"\n[bold]{folder.name}[/bold] {mode}  "
                f"tz=[cyan]{tz_disp}[/cyan] [dim]({plan.tz_reason})[/dim]"
            )
            if plan.tz_name is None and plan.candidates:
                console.print(
                    "  [yellow]⚠ no timezone signal[/yellow] — wall clock stored "
                    "as UTC; relative order is correct, absolute time is offset. "
                    "Re-run with --timezone <IANA> for exact instants."
                )
            for c in plan.candidates[:50]:
                tz_note = ""
                if c.tz_name and c.tz_name != plan.tz_name:
                    tz_note = f" [magenta]@{c.tz_name}[/magenta]"  # per-clip override
                console.print(
                    f"  [green]{c.local_date_time:%Y-%m-%d %H:%M:%S}[/green] "
                    f"[dim]{c.mode}[/dim] {c.media_path.name}{tz_note} "
                    f"[dim]← {c.source}[/dim]"
                )
            if len(plan.candidates) > 50:
                console.print(f"  [dim]… +{len(plan.candidates) - 50} more[/dim]")
            console.print(
                f"  [bold]{len(plan.candidates)}[/bold] datable | "
                f"[dim]{plan.already_dated} already dated, "
                f"{len(plan.no_date_source)} no date source, "
                f"{len(plan.unmatched)} not in Immich[/dim]"
            )
            grand_cands += len(plan.candidates)
            if apply and plan.candidates:
                written = backfill_dates_mod.apply_plan(conn, plan)
                grand_written += written
                console.print(f"  [green]✓ wrote {written} date(s)[/green]")
    finally:
        conn.close()

    if apply:
        console.print(
            f"\n[bold green]done[/bold green] — dated {grand_written} asset(s). "
            "Re-run `immy cluster` if you use auto-albums."
        )
    else:
        console.print(
            f"\n[bold]{grand_cands} asset(s) would be dated.[/bold] "
            "Re-run with [bold]--apply[/bold] to write."
        )


@app.command("find-duplicates")
def find_duplicates(
    path: Path = typer.Argument(..., help="Directory to scan."),
    snapshot_path: Path = typer.Option(
        Path.home() / ".immy" / "library-snapshot.sqlite", "--snapshot",
        help="SQLite snapshot produced by `immy snapshot`.",
    ),
    out: Path = typer.Option(
        None, "--out",
        help="Markdown report path (default: <path>/dupes.md). JSON lands next "
             "to it with the same stem.",
    ),
    fast: bool = typer.Option(
        False, "--fast",
        help="Skip SHA1 verification; name+size match lands as `likely`.",
    ),
    thorough: bool = typer.Option(
        False, "--thorough",
        help="Hash every file, catching renames. Slow — reads the whole tree.",
    ),
    min_size: int = typer.Option(
        duplicates_mod.DEFAULT_MIN_SIZE, "--min-size",
        help="Skip files smaller than this many bytes (default: 0).",
    ),
    ignore: list[str] = typer.Option(
        None, "--ignore",
        help="Extra glob(s) to ignore (repeatable). Defaults already cover "
             ".DS_Store, Thumbs.db, etc.",
    ),
    follow_symlinks: bool = typer.Option(
        False, "--follow-symlinks",
        help="Follow symlinks during the walk. Default off (loops + "
             "Time Machine snapshots).",
    ),
    into_bundles: bool = typer.Option(
        False, "--into-bundles",
        help="Descend into macOS bundles (*.photoslibrary, *.app). Off by "
             "default — usually slow and not useful.",
    ),
) -> None:
    """Report which files under PATH are already in the Immich snapshot.

    Tiers:
      - exact      — name + size + SHA1 match  (safe to delete locally)
      - likely     — name + size match, hash not checked / unavailable
      - name-only  — filename matches but size differs  (investigate)
      - no-match   — not in Immich  (candidates for ingest)

    Defaults are biased for speed on big backup disks: we only read file
    contents when `(name, size)` already matches. Pass `--thorough` to
    also catch renames (slow: reads the whole tree).
    """
    if fast and thorough:
        console.print("[red]--fast and --thorough are mutually exclusive[/red]")
        raise typer.Exit(code=2)

    if not snapshot_path.exists():
        console.print(
            f"[red]snapshot not found:[/red] {snapshot_path}\n"
            "Run `immy snapshot` first (needs Immich DB access)."
        )
        raise typer.Exit(code=2)

    if not path.exists() or not path.is_dir():
        console.print(f"[red]not a directory:[/red] {path}")
        raise typer.Exit(code=2)

    hash_mode = (
        duplicates_mod.HashMode.FAST if fast
        else duplicates_mod.HashMode.THOROUGH if thorough
        else duplicates_mod.HashMode.ON_MATCH
    )

    extra_ignore = tuple(ignore) if ignore else ()
    ignore_globs = duplicates_mod.DEFAULT_IGNORE_GLOBS + extra_ignore

    # Progress feedback. Rich's Live output would be nicer but a simple
    # tick-every-500 keeps the scan loop tight and pipe-friendly.
    counter = {"n": 0}

    def _tick(p: Path, r: duplicates_mod.ScanResult) -> None:
        counter["n"] += 1
        if counter["n"] % 500 == 0:
            console.print(
                f"  [dim]…{counter['n']:,} files scanned[/dim]",
                highlight=False,
            )

    console.print(
        f"[bold]find-duplicates[/bold] {path} "
        f"[dim](mode: {hash_mode.value}, snapshot: {snapshot_path})[/dim]"
    )
    try:
        summary = duplicates_mod.scan(
            path, snapshot_path,
            hash_mode=hash_mode,
            ignore_globs=ignore_globs,
            min_size=min_size,
            follow_symlinks=follow_symlinks,
            into_bundles=into_bundles,
            progress=_tick,
        )
    except snapshot_mod.IncompleteSnapshotError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    report_md = out if out else (path / "dupes.md")
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(duplicates_mod.render_markdown(summary, path))
    report_json = report_md.with_suffix(".json")
    import json as _json
    report_json.write_text(_json.dumps(
        duplicates_mod.to_json_rows(summary), indent=2,
    ))

    console.print()
    for v in duplicates_mod.Verdict:
        console.print(
            f"  {v.value:<10} {summary.count(v):>6,} files"
        )
    console.print(
        f"\n[green]✓[/green] report: [cyan]{report_md}[/cyan] "
        f"(+ {report_json.name})"
    )


@app.command("match")
def match(
    path: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Inbound media root to place against the library.",
    ),
    snapshot_path: Path = typer.Option(
        Path.home() / ".immy" / "library-snapshot.sqlite", "--snapshot",
        help="v2 SQLite snapshot produced by `immy snapshot`.",
    ),
    thorough: bool = typer.Option(
        False, "--thorough",
        help="Hash every file for dedup (catches renames). Slow — reads the "
             "whole tree. Default hashes only on a name+size match.",
    ),
    fast: bool = typer.Option(
        False, "--fast/--no-fast", "--no-verify",
        help="Trust a name+size match as a duplicate — skip SHA1 entirely. "
             "Turns a ~2 TB already-promoted tree from ~50 min into ~2 min, "
             "at the cost of missing a same-name-same-size-different-bytes file.",
    ),
    max_km: float = typer.Option(
        clustering_mod.DEFAULT_MAX_KM, "--max-km",
        help="Distance a clip may sit from a trip and still count as part of it.",
    ),
    max_gap_hours: float = typer.Option(
        clustering_mod.DEFAULT_MAX_GAP_HOURS, "--max-gap-hours",
        help="Date slack (hours) around a trip's range for placement.",
    ),
) -> None:
    """Place an inbound media dump against the existing Immich library.

    Read-only. For PATH (an inbound folder of about-to-be-imported media)
    it reports, per top-level subfolder AND per self-clustered event:
    which files are already in Immich (dedup), which belong to an existing
    trip (matched/extends), and which are new — reconstructing trips from
    immy-cluster albums + raw points in the snapshot. Drone/video clips
    without EXIF GPS fall back to date-only placement (lower confidence).

    Dedup hashes a file only when its (name, size) already matches the
    snapshot (`--fast`/`--no-verify` skips even that; `--thorough` hashes
    everything to catch renames). Needs a v2 snapshot (`immy snapshot`).
    """
    if fast and thorough:
        console.print("[red]--fast and --thorough are mutually exclusive[/red]")
        raise typer.Exit(code=2)
    if not snapshot_path.exists():
        console.print(
            f"[red]snapshot not found:[/red] {snapshot_path}\n"
            "Run `immy snapshot` first (needs Immich DB access)."
        )
        raise typer.Exit(code=2)

    db = _open_snapshot_or_exit(snapshot_path)
    try:
        try:
            snapshot_mod.require_schema(db)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=2)

        assets = snapshot_mod.read_assets(db)
        albums = snapshot_mod.read_albums(db)
        membership = snapshot_mod.read_album_membership(db)
        trips = match_mod.build_existing_trips(
            assets, albums, membership,
            max_gap_hours=max_gap_hours, max_km=max_km,
        )
        console.print(
            f"[bold]match[/bold] {path} "
            f"[dim](snapshot: {len(assets):,} assets, {len(trips)} trips — "
            f"{sum(1 for t in trips if t.source == 'album')} albums + "
            f"{sum(1 for t in trips if t.source == 'cluster')} clustered)[/dim]"
        )

        hash_mode = (
            duplicates_mod.HashMode.FAST if fast
            else duplicates_mod.HashMode.THOROUGH if thorough
            else duplicates_mod.HashMode.ON_MATCH
        )
        if fast:
            console.print(
                "  [dim]--fast: trusting name+size for dedup (no SHA1)[/dim]"
            )
        items = match_mod.scan_inbound(path, db, hash_mode=hash_mode)
        report = match_mod.build_report(
            items, trips, max_km=max_km, max_gap_hours=max_gap_hours,
        )
    finally:
        db.close()

    if report.total_files == 0:
        console.print("[yellow]no media files found under PATH[/yellow]")
        return

    # --- grouping A: per subfolder ---
    console.print("\n[bold]By subfolder[/bold]")
    for fr in report.folders:
        live = fr.total - fr.duplicates
        bits = []
        for verdict in ("matched", "extends", "new"):
            n = fr.placements.get(verdict, 0)
            if n:
                bits.append(f"{n} {verdict}")
        if fr.duplicates:
            bits.append(f"[dim]{fr.duplicates} dup[/dim]")
        span = " [magenta]⚠ spans multiple trips[/magenta]" if fr.spans_multiple else ""
        trips_disp = ""
        if fr.trips:
            shown = ", ".join(sorted(fr.trips)[:3])
            more = "…" if len(fr.trips) > 3 else ""
            trips_disp = f"  [cyan]→ {shown}{more}[/cyan]"
        console.print(
            f"  [bold]{fr.subfolder}[/bold] "
            f"[dim]({fr.total} files, {live} to place)[/dim]  "
            f"{' · '.join(bits) or '[dim]—[/dim]'}{trips_disp}{span}"
        )

    # --- grouping B: self-clustered events ---
    console.print("\n[bold]By event[/bold] [dim](self-clustered, GPS items)[/dim]")
    if not report.events:
        console.print("  [dim](no geo-clusterable events)[/dim]")
    for ev in report.events:
        s, e = ev.when_range
        when = (
            s.strftime("%Y-%m-%d") if s.date() == e.date()
            else f"{s:%Y-%m-%d}–{e:%Y-%m-%d}"
        )
        color = {"matched": "green", "extends": "yellow",
                 "new": "cyan", "duplicate": "dim"}.get(ev.placement.verdict, "white")
        conf = "" if ev.placement.confidence == "geo" else f" [dim]({ev.placement.confidence})[/dim]"
        console.print(
            f"  [dim]{when}[/dim] {ev.size:>3} clip(s)  "
            f"[{color}]{ev.placement.verdict}[/{color}] "
            f"[dim]{ev.placement.reason}[/dim]{conf}"
        )

    # --- dedup tally + caveat ---
    console.print(
        f"\n[bold]{report.duplicates}/{report.total_files}[/bold] already in "
        f"Immich · [bold]{report.total_files - report.duplicates}[/bold] to place"
        + (f" · [dim]{report.gps_less} GPS-less (date-only)[/dim]"
           if report.gps_less else "")
    )
    if report.gps_less:
        console.print(
            "  [dim]GPS-less clips (drone/video) placed by date window; their "
            "coords live in .SRT — run `immy srt geotag` to lift confidence.[/dim]"
        )


@app.command("apple-people")
def apple_people(
    photos_library: Path = typer.Option(
        Path.home() / "Pictures" / "Photos Library.photoslibrary",
        "--photos-db",
        help="Path to a `.photoslibrary` bundle, or directly to Photos.sqlite.",
    ),
    snapshot_path: Path = typer.Option(
        Path.home() / ".immy" / "library-snapshot.sqlite", "--snapshot",
        help="SQLite snapshot produced by `immy snapshot`.",
    ),
    min_faces: int = typer.Option(
        3, "--min-faces",
        help="Skip persons with fewer than this many faces in Apple Photos.",
    ),
    only: list[str] = typer.Option(
        None, "--only",
        help="Restrict to named person(s) (repeatable, exact full-name match).",
    ),
    apply_: bool = typer.Option(
        False, "--apply",
        help="Write to Immich: name the existing (unnamed) person cluster "
        "each Apple person's faces overlap, and attach any orphaned "
        "same-cluster faces. Never creates new Person rows and never "
        "renames an already-named person — see the plan preview first.",
    ),
    yes: bool = typer.Option(
        False, "--yes",
        help="Skip the confirmation prompt before writing (for non-interactive use).",
    ),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config."),
) -> None:
    """Preview (or, with `--apply`, write) Apple Photos face-tagging into Immich.

    Matches Apple's named face clusters to Immich assets, prints the match
    rate, then builds a plan against Immich's *own* existing (already-
    clustered-but-unnamed) `asset_face`/`person` rows: whichever unnamed
    cluster an Apple person's faces consistently overlap is named, and any
    unclustered faces on the same assets are attached to it. This retroactively
    names every other face already in that cluster too, not just the ones
    Apple tagged — see `apple_photos.build_person_plans` for the matching.

    Requires a fresh snapshot (`immy snapshot`) for the match phase, and (for
    `--apply`) a `pg:` block in the config to reach Immich's Postgres.
    """
    try:
        db_path = apple_photos_mod.resolve_db_path(photos_library)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    if not snapshot_path.exists():
        console.print(
            f"[red]snapshot not found:[/red] {snapshot_path}\n"
            "Run `immy snapshot` first (needs Immich DB access)."
        )
        raise typer.Exit(code=2)

    console.print(
        f"[bold]apple-people[/bold]\n"
        f"  photos: [cyan]{db_path}[/cyan]\n"
        f"  snapshot: [cyan]{snapshot_path}[/cyan]"
    )

    conn = apple_photos_mod.open_ro(db_path)
    try:
        persons = apple_photos_mod.read_named_persons(
            conn,
            min_faces=min_faces,
            only=set(only) if only else None,
        )
    finally:
        conn.close()

    if not persons:
        console.print("[yellow]no named persons found (or all below --min-faces).[/yellow]")
        raise typer.Exit(code=0)

    snap = _open_snapshot_or_exit(snapshot_path)
    try:
        matches = apple_photos_mod.match_to_snapshot(persons, snap)
        snap_meta = snapshot_mod.read_meta(snap)
    finally:
        snap.close()

    total_faces = sum(len(p.faces) for p in persons)
    total_matched = sum(len(m) for m in matches.values())
    total_assets = snap_meta.get("asset_count", "?")

    console.print(
        f"\n[bold]{len(persons)}[/bold] named person(s) with ≥{min_faces} faces — "
        f"[bold]{total_faces:,}[/bold] faces total. "
        f"Snapshot has {total_assets} asset(s)."
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("person")
    table.add_column("apple faces", justify="right")
    table.add_column("→ matched", justify="right")
    table.add_column("%", justify="right")
    for person in persons:
        m = len(matches.get(person.apple_pk, []))
        total = len(person.faces)
        pct = f"{100 * m / total:.0f}%" if total else "-"
        style = "green" if m else "dim"
        table.add_row(
            person.full_name,
            f"{total:,}",
            f"[{style}]{m:,}[/{style}]",
            pct,
        )
    console.print(table)
    console.print(
        f"\nTotal: [bold]{total_matched:,}[/bold] / {total_faces:,} faces "
        f"({100 * total_matched / total_faces:.0f}%) map to assets in Immich."
    )

    config = load_config(config_path)
    if config.pg is None:
        console.print(
            "\n[dim]No changes made. Add a `pg:` block to the config and "
            "pass --apply to write.[/dim]"
        )
        return

    try:
        pconn = pg_mod.connect(config.pg)
    except Exception as e:
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)

    try:
        asset_ids = sorted({m.immich_asset_id for ms in matches.values() for m in ms})
        raw_faces = pg_mod.fetch_existing_faces(pconn, asset_ids)
        existing_faces_by_asset = {
            asset_id: [apple_photos_mod.ExistingFace(*row) for row in rows]
            for asset_id, rows in raw_faces.items()
        }
        plans = apple_photos_mod.build_person_plans(persons, matches, existing_faces_by_asset)

        plan_table = Table(show_header=True, header_style="bold")
        plan_table.add_column("person")
        plan_table.add_column("target cluster")
        plan_table.add_column("votes", justify="right")
        plan_table.add_column("+orphans", justify="right")
        plan_table.add_column("conflicts", justify="right")
        plan_table.add_column("no-detect", justify="right")
        actionable = [p for p in plans if p.target_person_id is not None]
        for p in plans:
            target = p.target_person_id[:8] if p.target_person_id else "[dim]-[/dim]"
            votes = f"{p.target_votes}/{p.total_votes}" if p.total_votes else "-"
            plan_table.add_row(
                p.full_name, target, votes,
                str(len(p.orphan_face_ids)), str(len(p.conflicts)), str(p.no_detection),
            )
        console.print("\n[bold]--apply plan[/bold] (against Immich's existing face clusters):")
        console.print(plan_table)

        total_conflicts = sum(len(p.conflicts) for p in plans)
        if total_conflicts:
            console.print(
                f"[yellow]{total_conflicts} face(s) overlap a cluster already named "
                "something else — skipped, not overwritten.[/yellow]"
            )
        if not actionable:
            console.print(
                "\n[yellow]No cluster meets the confidence bar "
                f"(≥{apple_photos_mod.MIN_VOTES} votes, "
                f"≥{apple_photos_mod.MIN_CONFIDENCE:.0%} confidence) — "
                "nothing to apply.[/yellow]"
            )
            return

        if not apply_:
            console.print(
                "\n[dim]No changes made. Pass --apply to write the plan above.[/dim]"
            )
            return

        if not yes:
            confirmed = typer.confirm(
                f"\nName {len(actionable)} Immich person cluster(s) and attach "
                f"{sum(len(p.orphan_face_ids) for p in actionable)} orphaned face(s)?"
            )
            if not confirmed:
                console.print("[yellow]aborted, no changes made.[/yellow]")
                return

        named = 0
        attached = 0
        for p in actionable:
            if pg_mod.name_person(pconn, p.target_person_id, p.full_name):
                named += 1
            attached += pg_mod.attach_orphan_faces(pconn, p.orphan_face_ids, p.target_person_id)
        pconn.commit()
        console.print(
            f"\n[green]applied:[/green] named {named} person cluster(s), "
            f"attached {attached} orphaned face(s)."
        )
    finally:
        pconn.close()


@app.command()
def status(
    trip: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
    with_audit: bool = typer.Option(
        True, "--audit/--no-audit",
        help="Count pending audit findings (runs exiftool over the trip).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Where a trip is at: pending audit, process marker, journal phases,
    offline cache, staged derivatives, last heartbeat. Read-only."""
    import json as json_mod
    from datetime import datetime as _dt

    from . import status as status_mod

    info = status_mod.trip_status(trip, load_config(config_path), with_audit=with_audit)
    if as_json:
        print(json_mod.dumps(info, indent=2, default=str))
        return

    console.print(f"[bold]{trip.name}[/bold]  [dim]{info['audit_dir']}[/dim]", highlight=False)
    audit = info["audit"]
    if audit is not None:
        color = "red" if audit["high"] else ("yellow" if audit["medium"] else "green")
        console.print(
            f"  audit       [{color}]{audit['high']} HIGH, {audit['medium']} MEDIUM pending[/{color}]"
            f"  ({audit['applied']} applied, {audit['files']} files)", highlight=False,
        )
    proc = info["process"]
    if proc is None:
        console.print("  process     [dim]not processed[/dim]")
    else:
        when = _dt.fromtimestamp(proc["processed_at"]).strftime("%Y-%m-%d %H:%M") if proc["processed_at"] else "?"
        console.print(
            f"  process     {when} — {proc['assets']} assets "
            f"({proc['inserted']} inserted, {proc['already_present']} already present)",
            highlight=False,
        )
    if info["journal"]:
        parts = []
        for worker, rec in info["journal"].items():
            extra = f" [yellow]({len(rec['versions'])} versions)[/yellow]" if len(rec["versions"]) > 1 else ""
            parts.append(f"{worker} {rec['done']}{extra}")
        console.print("  journal     " + ", ".join(parts), highlight=False)
    else:
        console.print("  journal     [dim]empty[/dim]")
    off = info["offline"]
    if off["entries"]:
        color = "yellow" if off["pending"] else "green"
        console.print(
            f"  offline     [{color}]{off['pending']} pending[/{color}] / {off['entries']} "
            f"({off['synced']} synced)", highlight=False,
        )
    der = info["derivatives"]
    if der["present"] or der["missing"]:
        color = "red" if der["missing"] else "green"
        console.print(
            f"  derivs      [{color}]{der['present']} staged, {der['missing']} missing[/{color}]",
            highlight=False,
        )
    hb = info["heartbeat"]
    if hb is not None:
        alive = {True: "[green]running[/green]", False: "[dim]exited[/dim]", None: "[dim]pid ?[/dim]"}[hb["alive"]]
        where = f"{hb['index']}/{hb['total']} " if hb.get("total") else ""
        console.print(
            f"  heartbeat   {hb['phase']}:{hb['step']} {where}{hb.get('file') or ''} "
            f"— {hb['age_s']}s ago, {alive}", highlight=False,
        )


@app.command()
def doctor(
    config_path: Path = typer.Option(None, "--config", help="immy config path."),
) -> None:
    """Preflight: config, binaries, paths, Immich API, Postgres schema, CLIP
    dimension. Read-only. Exits 1 if any configured check fails."""
    from . import doctor as doctor_mod

    config = load_config(config_path)
    checks = doctor_mod.run_all(config)
    style = {
        doctor_mod.OK: "green", doctor_mod.WARN: "yellow",
        doctor_mod.FAIL: "red", doctor_mod.SKIP: "dim",
    }
    width = max(len(c.name) for c in checks)
    for c in checks:
        console.print(
            f"[{style[c.status]}]{c.status:>4}[/{style[c.status]}]  "
            f"{c.name:<{width}}  {c.detail}",
            highlight=False,
        )
    failed = sum(c.status == doctor_mod.FAIL for c in checks)
    if failed:
        console.print(f"[red]{failed} check(s) failed[/red]")
        raise typer.Exit(code=1)


MOVERS_LOCK_SUFFIX = ".movers.lock"


dedup_app = typer.Typer(
    help="Cross-source dedup (iCloud + Google Takeout → library/originals). "
    "Cascade: block → pHash → CLIP-confirm → decide.",
    no_args_is_help=True,
)


def _open_manifest(manifest_path: Path):
    from .dedup import manifest as manifest_mod

    return manifest_mod, manifest_mod.open_manifest(manifest_path)


_MANIFEST_OPT = typer.Option(
    ..., "--manifest", help="Path to manifest.sqlite (created if missing)."
)


@dedup_app.command("bootstrap")
def dedup_bootstrap(
    originals_root: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="library/originals — the already-canonical Immich corpus.",
    ),
    manifest_path: Path = _MANIFEST_OPT,
) -> None:
    """Seed the manifest with everything already in library/originals as
    `canonical` — the corpus new arrivals dedup against. Registers +
    fingerprints in one go; no file is ever moved by this command."""
    from .dedup import engine as engine_mod

    manifest_mod, conn = _open_manifest(manifest_path)
    result = manifest_mod.register(conn, "originals", originals_root)
    console.print(
        f"registered {result.new} new (+{result.already_known} known) from {originals_root}"
    )
    ok, failed = engine_mod.fingerprint_pending(
        conn, source="originals", progress=_dedup_progress
    )
    conn.execute(
        "UPDATE asset SET status=? WHERE source='originals' AND status=?",
        (manifest_mod.CANONICAL, manifest_mod.FINGERPRINTED),
    )
    conn.commit()
    console.print(f"[green]canonical corpus: {ok} fingerprinted[/green]"
                  + (f", [red]{failed} failed[/red]" if failed else ""))


@dedup_app.command("register")
def dedup_register(
    source: str = typer.Argument(help="Source name: icloud | google."),
    root: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True,
        help="Staging root to walk (e.g. staging/icloud).",
    ),
    manifest_path: Path = _MANIFEST_OPT,
    min_age_hours: float = typer.Option(
        0.0, "--min-age-hours",
        help="Skip files younger than this (incremental settle gate — "
        "icloudpd lands Live Photo pairs non-atomically).",
    ),
) -> None:
    """Walk a staging source and register unseen media files (fast, fs-only)."""
    manifest_mod, conn = _open_manifest(manifest_path)
    result = manifest_mod.register(conn, source, root, min_age_hours=min_age_hours)
    console.print(
        f"[green]{result.new} new[/green], {result.already_known} already known, "
        f"{result.skipped_young} skipped (younger than {min_age_hours}h)"
    )


def _dedup_progress(done: int, total: int) -> None:
    console.print(f"  fingerprint {done}/{total}", highlight=False)


@dedup_app.command("fingerprint")
def dedup_fingerprint(
    manifest_path: Path = _MANIFEST_OPT,
    source: str = typer.Option(None, "--source", help="Limit to one source."),
    batch_size: int = typer.Option(200, "--batch-size"),
    refresh_meta: bool = typer.Option(
        False, "--refresh-meta",
        help="Instead of fingerprinting new arrivals, re-read burst / Live Photo / "
        "edited ids for rows fingerprinted before maker notes were read "
        "(fills only NULLs; re-opens unapplied auto clusters that gain one).",
    ),
) -> None:
    """Extract metadata (exiftool batch) + pHash for every registered asset.
    Resumable: commits per batch, failed files land in status `error`."""
    from .dedup import engine as engine_mod

    if refresh_meta:
        _dedup_refresh_meta(engine_mod, manifest_path, batch_size)
        return
    _, conn = _open_manifest(manifest_path)
    stats: dict = {}
    ok, failed = engine_mod.fingerprint_pending(
        conn, source=source, batch_size=batch_size, progress=_dedup_progress,
        stats=stats,
    )
    color = "red" if failed else "green"
    console.print(
        f"[green]{ok} fingerprinted[/green], [{color}]{failed} failed[/{color}]"
        + (f", {stats['alias']} already in the library (alias)" if stats.get("alias") else "")
    )


def _dedup_refresh_meta(engine_mod, manifest_path: Path, batch_size: int) -> None:
    # It moves `decided` rows back to `clustered`, which `dedup apply` reads:
    # hold the movers lock so it never runs under an apply in progress.
    lock_path = manifest_path.with_suffix(MOVERS_LOCK_SUFFIX)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        console.print(
            f"[red]a file-moving run (dedup apply / promote-rest / triage apply) "
            f"looks to be in progress[/red] (lock file exists: {lock_path}). "
            f"If that's stale, delete it and retry."
        )
        raise typer.Exit(1)
    try:
        _, conn = _open_manifest(manifest_path)
        result = engine_mod.refresh_metadata(
            conn, batch_size=batch_size, progress=_dedup_progress,
        )
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
    console.print(
        f"[green]{result['updated']} updated[/green] of {result['checked']} checked · "
        f"missing {result['missing']} · unreadable {result['unreadable']} · "
        f"clusters reopened {result['clusters_reopened']} · "
        f"applied clusters affected {result['applied_clusters_affected']}"
    )
    if result["clusters_reopened"]:
        console.print("re-run `dedup decide` to re-decide the reopened clusters")
    flagged = result["needs_review"]
    if flagged:
        shown = ", ".join(str(c) for c in flagged[:50])
        more = f" (+{len(flagged) - 50} more)" if len(flagged) > 50 else ""
        console.print(
            f"[yellow]{len(flagged)} merge(s) not made by `decide` (a person's, or "
            f"from before that was recorded) now trip a burst / Live / edited guard; "
            f"left as they are — review them:[/yellow] clusters {shown}{more}"
        )


@dedup_app.command("index-library")
def dedup_index_library(
    originals_root: Path = typer.Option(
        ..., "--originals", exists=True, file_okay=False, resolve_path=True,
        help="library/originals — what Immich serves.",
    ),
    manifest_path: Path = _MANIFEST_OPT,
    subdir: list[str] = typer.Option(
        None, "--dir",
        help="Only these subtrees of --originals (repeatable), e.g. --dir 2026/05. "
        "Default: the whole library.",
    ),
    limit: int = typer.Option(None, "--limit", help="Hash at most this many files this run."),
) -> None:
    """Hash library files into the manifest's content index (`library_file`).

    This is what lets `fingerprint` recognise an arrival whose exact bytes
    the library already holds. Read-only on the files; resumable (unchanged
    files are not re-read); prunes index rows whose file is gone. For the
    Photos-bridge overlap window, index the recent YYYY/MM dirs only."""
    from .dedup import identity

    roots: list[Path] = []
    for d in subdir or ["."]:
        root = Path(os.path.normpath(originals_root / d))
        rel = root.relative_to(originals_root) if root.is_relative_to(originals_root) else None
        if rel is None or any(p.startswith(".") for p in rel.parts):
            console.print(f"[red]--dir must be a non-hidden subtree of --originals:[/red] {d}")
            raise typer.Exit(1)
        # Every component from --originals down must be a real directory: a
        # symlink anywhere (`link/subdir`) could lead outside the library.
        probe = originals_root
        for part in rel.parts:
            probe = probe / part
            if probe.is_symlink():
                console.print(f"[red]--dir crosses a symlink:[/red] {probe}")
                raise typer.Exit(1)
        if not root.is_dir():
            console.print(f"[red]not a directory:[/red] {root}")
            raise typer.Exit(1)
        roots.append(root)
    _, conn = _open_manifest(manifest_path)
    result = identity.index_library(
        conn, roots, limit=limit,
        progress=lambda n: console.print(f"  hashed {n}", highlight=False),
    )
    console.print(
        f"[green]{result.hashed} hashed[/green], {result.unchanged} unchanged, "
        f"{result.pruned} pruned, {result.skipped} skipped (unstable/unreadable/symlink)"
    )


@dedup_app.command("retry-errors")
def dedup_retry_errors(
    manifest_path: Path = _MANIFEST_OPT,
    match: str = typer.Option(
        None, "--match", help="Only errors whose message contains this, e.g. 'stub'.",
    ),
) -> None:
    """Send `error` rows back to `registered` (size/mtime refreshed from
    disk) so the next `fingerprint` retries them — e.g. a stub that has since
    been replaced by the real file. Rows whose file is gone stay `error`."""
    manifest_mod, conn = _open_manifest(manifest_path)
    reset = manifest_mod.retry_errors(conn, match=match)
    console.print(f"[green]{reset} reset to registered[/green]")


@dedup_app.command("cluster")
def dedup_cluster(manifest_path: Path = _MANIFEST_OPT) -> None:
    """Stage A+B: block by time/geo/stem, pHash-filter, form clusters."""
    from .dedup import engine as engine_mod

    _, conn = _open_manifest(manifest_path)
    result = engine_mod.cluster(conn)
    console.print(
        f"universe {result['universe']} → {result['pairs_blocked']} blocked pairs → "
        f"{result['pairs_confirmed']} confirmed → "
        f"[green]{result['clusters_created']} new cluster(s)[/green]"
        + (
            f", {result['clusters_extended']} extended (re-confirm needed)"
            if result["clusters_extended"] else ""
        )
    )
    for warning in result["warnings"]:
        console.print(f"[yellow]{warning}[/yellow]")


@dedup_app.command("confirm")
def dedup_confirm(
    manifest_path: Path = _MANIFEST_OPT,
    config_path: Path = typer.Option(None, "--config", help="immy config.yml (reads the `ml:` block)."),
) -> None:
    """Stage C: CLIP-confirm clusters `cluster` left ambiguous.

    Attaches `clip_cos_sim` (min cosine(winner, member) over image members)
    to every pending/review cluster missing one — cached per-asset in
    `embedding` so re-runs only pay for new clusters. Run this after
    `cluster`, before `decide` (or again later — `decide` re-reads
    `clip_cos_sim` on every pass, so it also picks up backfilled clusters).
    Doesn't move or decide anything by itself.
    """
    from .dedup import engine as engine_mod

    config = load_config(config_path)
    model_name = (
        config.ml.clip_model
        if (config.ml is not None and config.ml.clip_model)
        else clip_mod.DEFAULT_MODEL
    )
    backend = os.environ.get("IMMY_CLIP_BACKEND") or (
        config.ml.clip_backend if config.ml is not None else "mlx"
    )
    endpoint = os.environ.get("IMMY_IMMICH_ML_URL") or (
        config.ml.immich_ml_url if config.ml is not None else None
    )

    _, conn = _open_manifest(manifest_path)
    result = engine_mod.confirm_clip(
        conn, backend=backend, endpoint=endpoint, model_name=model_name,
        progress=_dedup_progress,
    )
    color = "red" if result["failed"] else "green"
    console.print(
        f"[green]{result['ok']} confirmed[/green], "
        f"[{color}]{result['failed']} failed[/{color}] "
        f"(of {result['total']} clusters needing CLIP)"
    )


@dedup_app.command("decide")
def dedup_decide(manifest_path: Path = _MANIFEST_OPT) -> None:
    """Stage D on pending/review clusters. Auto-merge needs strong pHash
    (or a CLIP Stage C confirm past the calibrated bar) + an agreeing
    metadata signal; guards (burst/Live/edited/crop) and anything softer
    route to review. No files are moved — promote is a separate, later
    command."""
    from .dedup import engine as engine_mod

    _, conn = _open_manifest(manifest_path)
    counts = engine_mod.decide(conn)
    console.print(
        f"[green]auto {counts['auto']}[/green] · review {counts['review']} · "
        f"kept_all {counts['kept_all']}"
    )


@dedup_app.command("apply")
def dedup_apply(
    manifest_path: Path = _MANIFEST_OPT,
    originals_root: Path = typer.Option(
        ..., "--originals", exists=True, file_okay=False, resolve_path=True,
        help="Immich external library root — winners land in <root>/<YYYY>/<MM>/.",
    ),
    quarantine_root: Path = typer.Option(
        ..., "--quarantine", file_okay=False, resolve_path=True,
        help="Losers land here, mirroring their staging path. Never purged by this command.",
    ),
    write: bool = typer.Option(False, "--write", help="Move files for real (default: dry-run report)."),
    limit: int = typer.Option(None, "--limit", help="Cap assets processed this run (testing/batching)."),
) -> None:
    """Stage E on `decide`'s `auto` clusters: promote winners into the
    Immich library, quarantine losers. Nothing before this command has
    ever moved a file — `decide` only writes a decision. Idempotent
    (asset.status advances on success, crash-safe mid-move) and resumable
    via --limit batching. Refuses to run if another `--write` apply is
    already in progress against this manifest."""
    from .dedup import engine as engine_mod

    quarantine_root.mkdir(parents=True, exist_ok=True)
    # One lock for every command that moves files the manifest tracks
    # (dedup apply, dedup promote-rest, triage apply): they read and write
    # the same rows and paths, so they must not interleave.
    lock_path = manifest_path.with_suffix(MOVERS_LOCK_SUFFIX)
    lock_fd = None
    if write:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            console.print(
                f"[red]another file-moving run (dedup apply / promote-rest / triage apply) "
                f"looks to be in progress[/red] "
                f"(lock file exists: {lock_path}). If that's stale (a prior run crashed "
                f"hard), delete the lock file and retry."
            )
            raise typer.Exit(1)

    try:
        _, conn = _open_manifest(manifest_path)
        result = engine_mod.apply_decisions(
            conn, originals_root=originals_root, quarantine_root=quarantine_root,
            dry_run=not write, limit=limit,
        )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

    prefix = "[yellow]dry-run[/yellow] " if not write else "[green]applied[/green] "
    console.print(
        f"{prefix}promote {result['promoted']} ({result['promoted_bytes'] / 1e9:.1f} GB) · "
        f"quarantine {result['quarantined']} ({result['quarantined_bytes'] / 1e9:.1f} GB) · "
        f"sidecars {result['sidecars_written']} · errors {result['errors']}"
        + (f" · losers held {result['losers_held']}" if result.get("losers_held") else "")
        + (
            f" · aliases quarantined {result['aliases_quarantined']}"
            f" ({result['aliases_bytes'] / 1e9:.1f} GB)"
            f", requeued {result['aliases_requeued']}"
            if result.get("aliases_quarantined") or result.get("aliases_requeued") else ""
        )
    )
    for sample in result["error_samples"]:
        console.print(f"[red]error:[/red] {sample}")
    for sample in result["held_samples"]:
        console.print(f"[yellow]held:[/yellow] {sample}")


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _warn_if_exposed(host: str) -> None:
    """The review/pano web UIs have no authentication: say so when the bind
    address is not loopback."""
    if host not in _LOOPBACK_HOSTS:
        console.print(
            f"[yellow]warning:[/yellow] binding {host} — this server has no "
            "authentication; anyone who can reach it can use it."
        )


@dedup_app.command("review-server")
def dedup_review_server(
    manifest_path: Path = _MANIFEST_OPT,
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Bind address. Default is loopback-only (the UI has no "
        "authentication). Inside docker pass `--host 0.0.0.0` so a `--publish` "
        "can reach it, and restrict exposure on the host side instead "
        "(`--publish 127.0.0.1:8765:8765`).",
    ),
    thumb_dir: Path = typer.Option(
        Path("/scratch/dedup-review-tool"), "--thumb-dir",
        help="Thumbnail cache root (lazily filled, never regenerated).",
    ),
) -> None:
    """Web UI for the human half of Stage D: walk `review` clusters
    safest-first, record merge / keep-all decisions straight into the
    manifest (same write path as `decide`). Never moves a file — `dedup
    apply` picks up the resulting `auto` clusters later. Runs in the
    foreground for the duration of a review session; Ctrl-C when done.

        sudo docker compose -f deploy/n5/compose.yaml run --rm \\
          --publish 127.0.0.1:8765:8765 \\
          immy dedup review-server --host 0.0.0.0 --manifest /state/manifest.sqlite

    then `ssh -L 8765:localhost:8765 n5` and open http://localhost:8765.
    """
    from .dedup import review as review_mod

    _warn_if_exposed(host)
    console.print(f"serving dedup review on http://{host}:{port} — Ctrl-C to stop")
    review_mod.serve(manifest_path, thumb_dir, host, port)


@dedup_app.command("promote-rest")
def dedup_promote_rest(
    manifest_path: Path = _MANIFEST_OPT,
    originals_root: Path = typer.Option(
        ..., "--originals", exists=True, file_okay=False, resolve_path=True,
        help="Immich external library root — keepers land in <root>/<YYYY>/<MM>/.",
    ),
    write: bool = typer.Option(False, "--write", help="Move files for real (default: dry-run report)."),
    limit: int = typer.Option(None, "--limit", help="Cap assets processed this run (smoke-testing/batching)."),
) -> None:
    """Stage F: after review is done, promote every remaining keeper out of
    staging — never-clustered singletons plus kept_all/review members.
    Quarantines nothing; `apply` remains the only command that does.
    Refuses to run alongside another file-moving run on this manifest."""
    from .dedup import engine as engine_mod

    # One lock for every command that moves files the manifest tracks
    # (dedup apply, dedup promote-rest, triage apply): they read and write
    # the same rows and paths, so they must not interleave.
    lock_path = manifest_path.with_suffix(MOVERS_LOCK_SUFFIX)
    lock_fd = None
    if write:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            console.print(
                f"[red]another file-moving run (dedup apply / promote-rest / triage apply) "
                f"looks to be in progress[/red] "
                f"(lock file exists: {lock_path}). If that's stale, delete it and retry."
            )
            raise typer.Exit(1)
    try:
        _, conn = _open_manifest(manifest_path)
        result = engine_mod.promote_rest(
            conn, originals_root=originals_root, dry_run=not write, limit=limit,
            progress=_dedup_progress,
        )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

    prefix = "[yellow]dry-run[/yellow] " if not write else "[green]promoted[/green] "
    console.print(
        f"{prefix}{result['promoted']} keepers ({result['promoted_bytes'] / 1e9:.1f} GB) · "
        f"sidecars {result['sidecars_written']} · errors {result['errors']}"
    )
    for sample in result["error_samples"]:
        console.print(f"[red]error:[/red] {sample}")


@dedup_app.command("rescore")
def dedup_rescore(
    manifest_path: Path = _MANIFEST_OPT,
    thumb_dir: Path = typer.Option(
        Path("/scratch/dedup-review-tool"), "--thumb-dir",
        help="Review tool's thumbnail cache (reused as the pixel source).",
    ),
    force: bool = typer.Option(False, "--force", help="Recompute existing scores."),
) -> None:
    """Compute pixel-identity signals (grayscale NCC + capture-time delta)
    for every remaining review cluster into the `review_signal` side table.
    Decision-support only — writes no decisions, moves no files. The review
    UI shows the scores and lets sweeps threshold on them: same-frame
    re-exports score ~1.0 even where pHash broke; distinct shots of the
    same scene drop well below."""
    from .dedup import signals as signals_mod

    _, conn = _open_manifest(manifest_path)
    result = signals_mod.compute_signals(
        conn, thumb_dir, progress=_dedup_progress, force=force
    )
    console.print(
        f"[green]{result['scored']} scored[/green], "
        f"{result['no_pixels']} without decodable pixels "
        f"(of {result['total']} clusters)"
    )


@dedup_app.command("status")
def dedup_status(manifest_path: Path = _MANIFEST_OPT) -> None:
    """Counts per source × status, cluster decisions, embedding cache."""
    manifest_mod, conn = _open_manifest(manifest_path)
    data = manifest_mod.stats(conn)
    table = Table(show_header=True, header_style="bold")
    table.add_column("source")
    statuses = sorted({s for per in data["assets"].values() for s in per})
    for status in statuses:
        table.add_column(status, justify="right")
    for source, per in sorted(data["assets"].items()):
        table.add_row(source, *(f"{per.get(s, 0):,}" or "" for s in statuses))
    console.print(table)
    if data["clusters"]:
        console.print("clusters: " + " · ".join(
            f"{decision}={count:,}" for decision, count in sorted(data["clusters"].items())
        ))
    console.print(f"cached embeddings: {data['embeddings']:,}")


app.add_typer(dedup_app, name="dedup")


triage_app = typer.Typer(
    help="Footage triage: signal scan + per-trip report for grading trip "
    "videos keep/compress/cold/trash. Never touches a file.",
    no_args_is_help=True,
)


@triage_app.command("scan")
def triage_scan(
    manifest_path: Path = _MANIFEST_OPT,
    config_path: Path = typer.Option(None, "--config", help="immy config.yml (ml: + pg: blocks)."),
    root: str = typer.Option(
        "/originals", "--root",
        help="Manifest path prefix of the originals library (asset.path anchor).",
    ),
    fs_root: str = typer.Option(
        None, "--fs-root",
        help="Where those files are readable from THIS process (host runs: "
        "/mnt/tank/immich/originals). Default: same as --root.",
    ),
    frames_dir: Path = typer.Option(
        Path("/scratch/triage-frames"), "--frames-dir",
        help="Sampled-frame cache root (6 JPEGs per clip, reused by the "
        "future review UI; safe to delete — frames re-extract on demand).",
    ),
    limit: int = typer.Option(None, "--limit", help="Cap NEW clips scanned this run (batching/smoke)."),
    force: bool = typer.Option(False, "--force", help="Re-scan clips that already have signals."),
    skip_immich: bool = typer.Option(
        False, "--skip-immich", help="Skip the favorite/album lookup (offline run)."
    ),
) -> None:
    """Gather per-clip signals for every trip video into `video_signal`:
    ffprobe, 6 sampled frames + a pooled CLIP vector (cached forever in
    `embedding`), Immich favorite/album flags, take-grouping, and
    conservative suggestions. Resumable: ^C and re-run any time; only
    new clips pay the probe/frames/CLIP cost."""
    from .triage import engine as triage_engine
    from .triage.flags import build_immich_lookup

    config = load_config(config_path)
    model_name = (
        config.ml.clip_model
        if (config.ml is not None and config.ml.clip_model)
        else clip_mod.DEFAULT_MODEL
    )
    backend = os.environ.get("IMMY_CLIP_BACKEND") or (
        config.ml.clip_backend if config.ml is not None else "mlx"
    )
    endpoint = os.environ.get("IMMY_IMMICH_ML_URL") or (
        config.ml.immich_ml_url if config.ml is not None else None
    )
    lookup = None if skip_immich else build_immich_lookup(config, root)
    if lookup is None and not skip_immich:
        console.print("[yellow]no pg/immich config — favorite/album flags will be NULL[/yellow]")

    _, conn = _open_manifest(manifest_path)
    result = triage_engine.scan(
        conn, root=root, fs_root=fs_root, frames_root=frames_dir,
        backend=backend, endpoint=endpoint, model_name=model_name,
        immich_lookup=lookup, force=force, limit=limit,
        progress=lambda done, total: console.print(
            f"  scan {done}/{total}", highlight=False
        ),
        log=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
    )
    console.print(
        f"[green]{result['scanned_now']} scanned[/green] "
        f"(+{result['total_scanned'] - result['scanned_now']} cached, "
        f"[red]{result['failed']} failed[/red]) of {result['eligible']} eligible · "
        f"{result['take_groups']} take groups · "
        f"{result['immich_flagged']} immich-flagged"
    )


@triage_app.command("report")
def triage_report(
    manifest_path: Path = _MANIFEST_OPT,
    root: str = typer.Option("/originals", "--root", help="Manifest originals prefix."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Per-trip rollup of scanned signals, biggest recoverable bytes first."""
    import json

    from .triage import engine as triage_engine

    _, conn = _open_manifest(manifest_path)
    data = triage_engine.report(conn, root=root)
    if as_json:
        console.print_json(json.dumps(data))
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("trip", "clips", "GB", "take GB", "compress GB", "favs"):
        table.add_column(col, justify="right" if col != "trip" else "left")
    for trip, t in data["trips"].items():
        table.add_row(
            trip, f"{t['clips']:,}", f"{t['bytes'] / 1e9:.1f}",
            f"{t['take_bytes'] / 1e9:.1f}", f"{t['compress_bytes'] / 1e9:.1f}",
            str(t["favorites"]),
        )
    tot = data["totals"]
    table.add_row(
        "[bold]total[/bold]", f"[bold]{tot['clips']:,}[/bold]",
        f"[bold]{tot['bytes'] / 1e9:.1f}[/bold]",
        f"[bold]{tot['take_bytes'] / 1e9:.1f}[/bold]",
        f"[bold]{tot['compress_bytes'] / 1e9:.1f}[/bold]",
        f"[bold]{tot['favorites']}[/bold]",
    )
    console.print(table)


@triage_app.command("apply")
def triage_apply(
    manifest_path: Path = _MANIFEST_OPT,
    config_path: Path = typer.Option(None, "--config", help="immy config.yml (for the Immich rescan)."),
    root: str = typer.Option("/originals", "--root"),
    fs_root: str = typer.Option(None, "--fs-root"),
    write: bool = typer.Option(False, "--write", help="Touch files for real (default: dry-run count)."),
    limit: int = typer.Option(None, "--limit", help="Cap clips processed this run."),
    threads: int = typer.Option(8, "--threads", help="Encoder thread cap (thermal budget)."),
    smallest_first: bool = typer.Option(
        False, "--smallest-first", help="Process smallest clips first (fast smoke runs)."
    ),
    ingest: Path = typer.Option(
        None, "--ingest",
        help="Instead of encoding locally, ingest finished encodes from this "
        "returns directory (external GPU worker): same verification and "
        "swap, no ffmpeg encode. Run repeatedly while the worker produces.",
    ),
) -> None:
    """Execute pending `compress` verdicts: re-encode (mp4→SVT-AV1,
    mov→x265, container never changes), verify duration, swap in place
    with the original quarantined, stamp `applied_at`, then one Immich
    library rescan. Resumable — ^C between clips loses nothing; biggest
    files go first so an interrupted run still banked the largest wins.

        sudo docker compose -f deploy/n5/compose.yaml run --rm --cpus 8 \\
          immy triage apply --manifest /state/manifest.sqlite \\
          --config /config/config.yml --write
    """
    from .triage import executor as executor_mod

    # One lock for every command that moves files the manifest tracks
    # (dedup apply, dedup promote-rest, triage apply): they read and write
    # the same rows and paths, so they must not interleave.
    lock_path = manifest_path.with_suffix(MOVERS_LOCK_SUFFIX)
    lock_fd = None
    if write:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            console.print(
                f"[red]another file-moving run (dedup apply / promote-rest / triage apply) "
                f"looks to be in progress[/red] "
                f"(lock file exists: {lock_path}). If that's stale, delete it and retry."
            )
            raise typer.Exit(1)
    try:
        _, conn = _open_manifest(manifest_path)
        kwargs = dict(
            root=root, fs_root=fs_root, dry_run=not write,
            progress=lambda i, n, name: console.print(
                f"  [{i}/{n}] {name}", highlight=False
            ),
            log=lambda msg: console.print(f"  {msg}", highlight=False),
        )
        if ingest is not None:
            result = executor_mod.apply_ingest(conn, returns_root=ingest, **kwargs)
        else:
            result = executor_mod.apply_compress(
                conn, threads=threads, limit=limit,
                smallest_first=smallest_first, **kwargs,
            )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)

    if not write:
        console.print(
            f"[yellow]dry-run[/yellow] — {result.processed} clips "
            f"({result.bytes_in / 1e9:.1f} GB) pending compress; re-run with --write"
        )
        return
    saved = (result.bytes_in - result.bytes_out) / 1e9
    console.print(
        f"[green]{result.swapped} swapped[/green] "
        f"({result.bytes_in / 1e9:.1f} → {result.bytes_out / 1e9:.1f} GB, "
        f"saved {saved:.1f} GB) · {result.no_gain} no-gain kept · "
        + (f"[red]{result.failed} failed[/red]" if result.failed else "0 failed")
    )
    config = load_config(config_path)
    if result.swapped and config.immich is not None and config.immich.library_id:
        client = ImmichClient(
            url=config.immich.url, api_key=config.immich.api_key,
            ssh_host=config.immich.ssh_host,
        )
        try:
            client.scan_library(config.immich.library_id)
            console.print("immich library rescan queued")
        except Exception as e:
            console.print(f"[yellow]immich rescan failed (queue it by hand): {e}[/yellow]")


@triage_app.command("stack-insv")
def triage_stack_insv(
    config_path: Path = typer.Option(None, "--config", help="immy config.yml (pg: + immich: blocks)."),
    write: bool = typer.Option(False, "--write", help="Create the stacks for real (default: dry-run report)."),
) -> None:
    """Backfill Immich stacks for Insta360 recordings: fold each recording's
    VID_ _00_/_10_ lens masters, LRV_ _11_ stitched preview, and any stitched
    .mp4 export into one stack (best-watchable member as primary). Groups
    where any member is already stacked are skipped. Reads asset ids from
    Postgres, writes only through the Immich API."""
    from . import stacks as stacks_mod
    from .pg import connect

    config = load_config(config_path)
    if config.pg is None or config.immich is None:
        console.print("[red]needs pg: and immich: config blocks[/red]")
        raise typer.Exit(1)
    with connect(config.pg) as pg_conn:
        rows = stacks_mod.fetch_candidates(pg_conn)
    plans, already, singles = stacks_mod.plan_stacks(rows)
    console.print(
        f"{len(rows)} insta360 files → {len(plans)} stacks to create · "
        f"{already} groups already stacked · {singles} singletons"
    )
    for plan in plans[:12]:
        console.print(
            f"  {plan.primary[1]} ← {', '.join(n for _, n in plan.children)}"
        )
    if len(plans) > 12:
        console.print(f"  … and {len(plans) - 12} more")
    if not write:
        console.print("[yellow]dry-run[/yellow] — re-run with --write to create them")
        return
    client = ImmichClient(
        url=config.immich.url,
        api_key=config.immich.api_key,
        ssh_host=config.immich.ssh_host,
    )
    done, failed = stacks_mod.apply_stacks(
        client, plans, log=lambda msg: console.print(f"[red]{msg}[/red]")
    )
    console.print(f"[green]{done} stacks created[/green]" +
                  (f" · [red]{failed} failed[/red]" if failed else ""))


@triage_app.command("review-server")
def triage_review_server(
    manifest_path: Path = _MANIFEST_OPT,
    port: int = typer.Option(8766, "--port"),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Bind address. Default is loopback-only (the UI has no "
        "authentication). Inside docker pass `--host 0.0.0.0` so a `--publish` "
        "can reach it, and restrict exposure on the host side instead "
        "(`--publish <tailscale-ip>:8766:8766`).",
    ),
    frames_dir: Path = typer.Option(
        Path("/scratch/triage-frames"), "--frames-dir",
        help="The scan's sampled-frame cache (contact-sheet source).",
    ),
    root: str = typer.Option(
        "/originals", "--root",
        help="Manifest path prefix of the originals library.",
    ),
    fs_root: str = typer.Option(
        None, "--fs-root",
        help="Where originals are readable from THIS process (host runs: "
        "/mnt/tank/immich/originals). Default: same as --root.",
    ),
) -> None:
    """Web UI for grading trip footage: one trip per screen, clips in
    capture order grouped into takes, K/C/A/T verdicts written to the
    `triage` table. Never touches a media file — the (future) executor
    is the only thing that acts on verdicts. Foreground; Ctrl-C when done.

        sudo docker compose -f deploy/n5/compose.yaml run --rm \\
          --name immy-triage-review --publish 100.115.236.50:8766:8766 \\
          immy triage review-server --host 0.0.0.0 --manifest /state/manifest.sqlite

    then open http://n5.bee-ruffe.ts.net:8766 from anywhere on the tailnet.
    """
    from .triage import review as review_mod

    _warn_if_exposed(host)
    console.print(f"serving triage review on http://{host}:{port} — Ctrl-C to stop")
    review_mod.serve(manifest_path, frames_dir, root, fs_root, host, port)


@app.command("pano-server")
def pano_server(
    manifest_path: Path = _MANIFEST_OPT,
    port: int = typer.Option(8767, "--port"),
    host: str = typer.Option(
        "127.0.0.1", "--host",
        help="Bind address. Default is loopback-only (no authentication); "
        "inside docker pass `--host 0.0.0.0`.",
    ),
    poster_dir: Path = typer.Option(
        Path("/scratch/pano-posters"), "--poster-dir",
        help="Lazily-filled poster cache (one JPEG per recording).",
    ),
    root: str = typer.Option("/originals", "--root"),
    fs_root: str = typer.Option(None, "--fs-root"),
) -> None:
    """360 viewer Immich doesn't have: per-trip grid of Insta360 recordings
    with a drag-around WebGL equirect player, streaming the stitched
    in-camera previews (LRV) and full-res exports. Read-only.

        sudo docker compose -f deploy/n5/compose.yaml run --rm \\
          --name immy-360-viewer --publish 100.115.236.50:8767:8767 \\
          immy pano-server --host 0.0.0.0 --manifest /state/manifest.sqlite
    """
    from . import pano as pano_mod

    _warn_if_exposed(host)
    console.print(f"serving 360 viewer on http://{host}:{port} — Ctrl-C to stop")
    pano_mod.serve(manifest_path, poster_dir, root, fs_root, host, port)


app.add_typer(triage_app, name="triage")


takeout_app = typer.Typer(
    help="Repairs for Google Takeout imports.",
    no_args_is_help=True,
)


@takeout_app.command("redate")
def takeout_redate(
    manifest_path: Path = typer.Option(..., "--manifest", help="The dedup manifest.sqlite the Takeout import went through."),
    takeout_root: Path = typer.Option(
        ..., "--takeout-root",
        help="Where the Takeout tree (its *.json companions are enough) lives now; "
             "stands in for --staging-prefix in the manifest's paths.",
    ),
    staging_prefix: str = typer.Option(
        "/staging/google-takeout", "--staging-prefix",
        help="The manifest's path prefix for the Takeout tree.",
    ),
    originals: Path = typer.Option(
        None, "--originals",
        help="The library's import path as seen from here, where sidecars are "
             "written (default: config originals_root).",
    ),
    import_path: str = typer.Option(
        None, "--import-path",
        help="The Immich-side import path that --originals is (default: the "
             "library's only import path). Assets under any other path are skipped.",
    ),
    placeholders: bool = typer.Option(
        True, "--placeholders/--no-placeholders",
        help="Re-date assets stamped with a placeholder (on-the-hour time shared by many assets).",
    ),
    utc: bool = typer.Option(
        True, "--utc/--no-utc",
        help="Re-zone Takeout assets Immich shows in UTC (right instant, wrong clock).",
    ),
    placeholder_min: int = typer.Option(
        None, "--placeholder-min",
        help="How many assets must share an on-the-hour time for it to count as a "
             "placeholder (default: trips.placeholder_min, else 10). Lower it to "
             "reach the stragglers; a source must still sit in that year's folder.",
    ),
    stack_copies: bool = typer.Option(
        True, "--stack-copies/--no-stack-copies",
        help="Stack each re-dated Takeout copy onto the library original it duplicates.",
    ),
    owner: str = typer.Option(None, "--owner", help="Immich user email (required with several users)."),
    only: list[str] = typer.Option(None, "--asset", help="Limit to these Immich asset ids (repeatable): a pilot run."),
    csv_path: Path = typer.Option(None, "--csv", help="Write the per-asset plan here."),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Default: plan only."),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Fix capture dates of Takeout files from their JSON companions.

    Finds each asset's Takeout source through the dedup manifest, reads the
    JSON's `photoTakenTime`, puts it on the local clock (file GPS → JSON
    geoData → nearby shots → UTC) and, with `--apply`, writes it to the XMP
    sidecar, registers the sidecar on the asset and has Immich refresh its
    metadata. A file with no JSON is dated from its numbered neighbours.
    Originals are never touched; every change is logged for undo.
    """
    import csv
    import json
    import sqlite3
    from collections import Counter
    from datetime import datetime as _dt, timedelta as _td

    from . import sidecar as sidecar_mod
    from . import takeout_redate as tr

    config = load_config(config_path)
    if config.pg is None:
        console.print("[red]no pg: block in immy config[/red]")
        raise typer.Exit(code=2)
    if not dry_run and config.immich is None:
        console.print("[red]no immich: block in immy config[/red] — --apply needs the API.")
        raise typer.Exit(code=2)
    originals = originals or config.originals_root
    if not dry_run and originals is None:
        console.print("[red]--originals (or originals_root) is needed to write sidecars[/red]")
        raise typer.Exit(code=2)
    if not manifest_path.is_file():
        console.print(f"[red]no manifest at {manifest_path}[/red]")
        raise typer.Exit(code=2)

    mconn = sqlite3.connect(f"file:{manifest_path}?mode=ro", uri=True)
    has_fix = mconn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='date_fix'").fetchone()
    rows = mconn.execute(
        "SELECT a.id, a.path, a.taken_at, "
        + ("df.old_taken_at " if has_fix else "NULL ")
        + "FROM asset a "
        + ("LEFT JOIN date_fix df ON df.asset_id = a.id " if has_fix else "")
        + "WHERE a.source = 'google' AND a.status = 'promoted'"
    ).fetchall()
    mconn.close()
    index = tr.manifest_index(rows)

    conn = pg_mod.connect(config.pg)
    cur = conn.cursor()
    cur.execute('SELECT id, email FROM "user" WHERE "deletedAt" IS NULL')
    users = cur.fetchall()
    match = [u for u in users if not owner or u[1] == owner]
    if len(match) != 1:
        console.print("[red]pass --owner <email>[/red]" if match else f"[red]no Immich user {owner!r}[/red]")
        raise typer.Exit(code=2)
    owner_id = str(match[0][0])
    cur.execute('SELECT "importPaths" FROM library WHERE "deletedAt" IS NULL')
    import_paths = sorted({p.rstrip("/") for (paths,) in cur.fetchall() for p in (paths or [])})
    # Every path below is relative to ONE import root, the one --originals
    # shows from here, so a sidecar can never be written under one root and
    # registered under another.
    root = (import_path or "").rstrip("/") or (import_paths[0] if len(import_paths) == 1 else None)
    if root is None:
        console.print(f"[red]{len(import_paths)} import paths[/red] ({', '.join(import_paths)}) — "
                      "pass --import-path for the one --originals points at.")
        raise typer.Exit(code=2)

    def rel_of(path: str) -> str | None:
        return path[len(root) + 1:] if path.startswith(root + "/") else None

    select = """
        SELECT a.id, a."originalPath", a."localDateTime" AT TIME ZONE 'UTC',
               e.latitude, e.longitude, %(reason)s
        FROM asset a JOIN asset_exif e ON e."assetId" = a.id
        WHERE a."deletedAt" IS NULL AND a."ownerId" = %(owner)s
    """
    found: dict[str, tuple] = {}
    if placeholders:
        cur.execute(
            "WITH " + trips_mod._PLACEHOLDER_CTE.strip() + select
            + ' AND a."localDateTime" IN (SELECT t FROM placeholder)',
            {"owner": owner_id, "reason": "placeholder",
             "placeholder_min": placeholder_min or (config.trips and config.trips.placeholder_min)
             or trips_mod.DEFAULT_PLACEHOLDER_MIN},
        )
        found.update({str(r[0]): r for r in cur.fetchall()})
    if utc:
        cur.execute(select + """ AND e."timeZone" IN ('UTC', 'UTC+0', 'Etc/UTC', 'UTC+00:00')""",
                    {"owner": owner_id, "reason": "utc"})
        for r in cur.fetchall():
            found.setdefault(str(r[0]), r)
    targets = []
    for aid, path, local, lat, lon, reason in found.values():
        aid = str(aid)
        if only and aid not in only:
            continue
        rel = rel_of(path)
        if rel is None or (reason == "utc" and rel not in index):
            continue  # a UTC asset that isn't a Takeout import is not ours
        targets.append(tr.Target(aid, rel, local, lat, lon, reason))

    def neighbour_zone(instant, asset_id):
        # The zone most shots within 3 h carry; failing that, within a day.
        for hours in (3, 24):
            cur.execute("""
                SELECT e."timeZone", count(*) FROM asset a
                JOIN asset_exif e ON e."assetId" = a.id
                WHERE a."deletedAt" IS NULL AND a."ownerId" = %(owner)s AND a.id <> %(id)s
                  AND a."fileCreatedAt" BETWEEN %(t)s - make_interval(hours => %(h)s)
                                            AND %(t)s + make_interval(hours => %(h)s)
                  AND e."timeZone" IS NOT NULL
                  AND e."timeZone" NOT IN ('UTC', 'UTC+0', 'Etc/UTC', 'UTC+00:00')
                GROUP BY 1 ORDER BY 2 DESC LIMIT 1
            """, {"owner": owner_id, "id": asset_id, "t": instant, "h": hours})
            row = cur.fetchone()
            if row:
                return row[0]
        return None

    fixes = tr.plan(targets, index, takeout_root=takeout_root,
                    staging_prefix=staging_prefix, neighbour_zone=neighbour_zone,
                    library_root=originals)
    good = [f for f in fixes if f.problem is None]

    # Twins: a library original this Takeout copy duplicates. Placeholder
    # assets are never twins: their own date is the fake one.
    fake = {t.asset_id for t in targets if t.reason == "placeholder"}
    twins: dict[str, tuple[str, str | None]] = {}
    if stack_copies:
        for f in good:
            if f.target.reason != "placeholder":
                continue
            name = Path(f.target.rel).name
            orig = tr.original_name(name)
            if orig == name:
                continue
            cur.execute("""
                SELECT a.id, a."fileCreatedAt", a."stackId", s2.embedding <=> s1.embedding
                FROM asset a
                LEFT JOIN smart_search s2 ON s2."assetId" = a.id
                LEFT JOIN smart_search s1 ON s1."assetId" = %(copy)s
                WHERE a."deletedAt" IS NULL AND a."ownerId" = %(owner)s
                  AND a."originalFileName" = %(orig)s AND a.id <> %(copy)s
            """, {"copy": f.target.asset_id, "owner": owner_id, "orig": orig})
            cands = [tr.Candidate(str(r[0]), r[1], str(r[2]) if r[2] else None,
                                  float(r[3]) if r[3] is not None else None)
                     for r in cur.fetchall() if str(r[0]) not in fake]
            twin = tr.pick_twin(f.taken.instant, cands)
            if twin:
                twins[f.target.asset_id] = (twin.asset_id, twin.stack_id)

    # --- report ---
    by = Counter()
    for f in fixes:
        if f.problem:
            by[("problem", f.problem)] += 1
        else:
            by[(f.target.reason, f.taken.source, f.zone_source)] += 1
    console.print(
        f"[bold]takeout redate[/bold] — {len(targets)} asset(s): "
        f"{sum(1 for t in targets if t.reason == 'placeholder')} placeholder, "
        f"{sum(1 for t in targets if t.reason == 'utc')} UTC → {len(good)} fixable"
        + (f", {len(twins)} copies to stack" if stack_copies else "")
    )
    for k, n in sorted(by.items(), key=lambda kv: -kv[1]):
        console.print(f"  {n:5}  {' · '.join(k)}")
    if csv_path:
        with open(csv_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["asset_id", "path", "reason", "old_local", "new_local", "zone",
                        "zone_source", "date_source", "staging", "twin", "problem"])
            for f in fixes:
                w.writerow([
                    f.target.asset_id, f.target.rel, f.target.reason,
                    f.target.local_date.isoformat(sep=" "),
                    f.local.isoformat(sep=" ") if f.local else "",
                    tr.zone_label(f.zone) if f.zone else ("UTC" if f.taken else ""),
                    f.zone_source, f.taken.source if f.taken else "", f.staging or "",
                    (twins.get(f.target.asset_id) or ("",))[0], f.problem or "",
                ])
        console.print(f"wrote {csv_path}")
    if dry_run or not good:
        if dry_run:
            console.print(f"\n[yellow]dry-run[/yellow] — pass `--apply` to write {len(good)} sidecar(s).")
        conn.close()
        return

    # --- apply ---
    state = config.state_root or Path.home() / ".immy"
    state.mkdir(parents=True, exist_ok=True)
    log_path = state / f"takeout-redate-{_dt.now().strftime('%Y%m%dT%H%M%S')}.jsonl"
    cur.execute(
        """SELECT "assetId", path FROM asset_file WHERE type = 'sidecar' AND "assetId" = ANY(%s)""",
        ([f.target.asset_id for f in good],),
    )
    registered = {str(a): p for a, p in cur.fetchall()}
    written: list[str] = []
    with open(log_path, "w") as log:
        for f in good:
            aid = f.target.asset_id
            immich_original = root + "/" + f.target.rel
            immich_sidecar = registered.get(aid) or immich_original + ".xmp"
            sidecar_rel = rel_of(immich_sidecar)
            if sidecar_rel is None:
                console.print(f"  [yellow]skipped[/yellow] {f.target.rel}: its sidecar "
                              f"{immich_sidecar} is outside {root}")
                continue
            local_sidecar = originals / sidecar_rel
            before = local_sidecar.read_text() if local_sidecar.is_file() else None
            try:
                sidecar_mod.write(originals / f.target.rel, {"DateTimeOriginal": f.xmp},
                                  xmp_path=local_sidecar)
            except RuntimeError as e:
                console.print(f"  [red]sidecar failed[/red] {f.target.rel}: {e}")
                continue
            cur.execute("""
                INSERT INTO asset_file ("assetId", type, path) VALUES (%s, 'sidecar', %s)
                ON CONFLICT ("assetId", type, "isEdited")
                DO UPDATE SET path = EXCLUDED.path
            """, (aid, immich_sidecar))
            conn.commit()
            log.write(json.dumps({
                "asset_id": aid, "sidecar": str(local_sidecar), "sidecar_before": before,
                "registered_before": registered.get(aid), "registered_now": immich_sidecar,
                "old_local": f.target.local_date.isoformat(), "new": f.xmp,
            }) + "\n")
            written.append(aid)
    client = ImmichClient(url=config.immich.url, api_key=config.immich.api_key,
                          ssh_host=config.immich.ssh_host)
    client.refresh_metadata(written)
    console.print(f"[green]✓[/green] {len(written)} sidecar(s) written and registered, "
                  f"metadata refresh queued. Undo log: {log_path}")

    stacked = 0
    for copy_id, (twin_id, stack_id) in twins.items():
        if copy_id not in written:
            continue
        ids = [twin_id, copy_id]
        if stack_id:
            cur.execute('SELECT "primaryAssetId" FROM stack WHERE id = %s', (stack_id,))
            primary = str(cur.fetchone()[0])
            cur.execute('SELECT id FROM asset WHERE "stackId" = %s AND "deletedAt" IS NULL', (stack_id,))
            members = [str(r[0]) for r in cur.fetchall()]
            ids = [primary] + [m for m in members if m != primary] + [copy_id]
        try:
            if client.create_stack(ids[0], ids[1:]):
                stacked += 1
        except ImmichError as e:
            console.print(f"  [red]stack failed[/red] {copy_id}: {e}")
    if twins:
        console.print(f"[green]✓[/green] {stacked} cop(ies) stacked under their originals")
    conn.close()


app.add_typer(takeout_app, name="takeout")


photos_app = typer.Typer(
    help="Apple Photos.app → Immich bridge. Uses the Mac's own Photos/iCloud "
    "session, so n5 never logs into iCloud.",
    no_args_is_help=True,
)


@photos_app.command("diff")
def photos_diff(
    since: str = typer.Option(
        None, "--since",
        help="Only assets ADDED to Photos on/after this date (YYYY-MM-DD). "
        "Default: 120 days ago.",
    ),
    all_: bool = typer.Option(False, "--all", help="Whole library, no --since cut."),
    photos_library: Path = typer.Option(
        Path.home() / "Pictures" / "Photos Library.photoslibrary",
        "--photos-db",
        help="Path to a `.photoslibrary` bundle, or directly to Photos.sqlite.",
    ),
    snapshot_path: Path = typer.Option(
        Path.home() / ".immy" / "library-snapshot.sqlite", "--snapshot",
        help="SQLite snapshot produced by `immy snapshot`.",
    ),
    out: Path = typer.Option(
        Path.home() / ".immy" / "photos-missing.txt", "--out",
        help="Write missing asset UUIDs here, one per line "
        "(feeds `osxphotos export --uuid-from-file`).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List Photos.app assets that Immich does not have yet. Read-only.

    Compares `Photos.sqlite` with an `immy snapshot` — no downloads, no
    iCloud traffic, nothing written except the UUID list. An asset counts as
    present on (filename + size) or on capture time within ±1 s; filename
    alone is not enough (IMG_NNNN counters repeat). See `immy.photos_diff`.
    """
    import json
    from datetime import datetime, timedelta, timezone
    from collections import Counter
    from . import photos_diff as pd

    if all_ and since:
        console.print("[red]--all and --since are mutually exclusive[/red]")
        raise typer.Exit(code=2)
    if all_:
        added_since = None
    elif since:
        try:
            added_since = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError:
            console.print(f"[red]bad --since date:[/red] {since}")
            raise typer.Exit(code=2)
    else:
        added_since = datetime.now(timezone.utc) - timedelta(days=120)

    try:
        db_path = apple_photos_mod.resolve_db_path(photos_library)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    if not snapshot_path.exists():
        console.print(
            f"[red]snapshot not found:[/red] {snapshot_path}\n"
            "Run `immy snapshot` first (needs Immich DB access)."
        )
        raise typer.Exit(code=2)

    snap = snapshot_mod.open_for_read(snapshot_path)
    created = snapshot_mod.read_meta(snap).get("created_at")
    age_h = None
    if created:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds() / 3600
    photos = pd.open_live_ro(db_path)
    try:
        result = pd.diff(photos, snap, added_since)
    finally:
        photos.close()
        snap.close()

    missing = result.by_status("missing")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{a.uuid}\n" for a in missing))

    counts = result.counts()
    missing_bytes = sum(a.size_bytes or 0 for a in missing)
    by_month = Counter(pd.month(a.created_unix) for a in missing)
    by_kind = Counter(a.kind for a in missing)
    by_camera = Counter(a.camera or "(none)" for a in missing)

    if as_json:
        print(json.dumps({
            "added_since": added_since.date().isoformat() if added_since else None,
            "snapshot_created_at": created,
            "counts": {"exact": counts["exact"], "time": counts["time"],
                       "missing": counts["missing"],
                       "skipped_hidden_bursts": result.skipped_hidden_bursts},
            "missing_bytes": missing_bytes,
            "missing_by_capture_month": dict(sorted(by_month.items())),
            "missing_by_kind": dict(by_kind),
            "missing_by_camera": dict(by_camera.most_common()),
            "out": str(out),
        }, indent=2))
        return

    scope = f"added since {added_since.date()}" if added_since else "whole library"
    console.print(
        f"[bold]photos diff[/bold] ({scope})\n"
        f"  photos:   [cyan]{db_path}[/cyan]\n"
        f"  snapshot: [cyan]{snapshot_path}[/cyan]"
        + (f" [dim]({age_h:.0f} h old)[/dim]" if age_h is not None else "")
    )
    if age_h is not None and age_h > 24:
        console.print(
            "  [yellow]snapshot is over a day old — anything Immich got since "
            "will show as missing. Re-run `immy snapshot`.[/yellow]"
        )

    total = len(result.items)
    t = Table(show_header=True, header_style="bold")
    t.add_column("status")
    t.add_column("assets", justify="right")
    t.add_row("in Immich (filename + size)", f"{counts['exact']:,}")
    t.add_row("in Immich (capture time ±1 s)", f"{counts['time']:,}")
    t.add_row("[bold]missing[/bold]", f"[bold]{counts['missing']:,}[/bold]")
    t.add_row("[dim]total[/dim]", f"[dim]{total:,}[/dim]")
    console.print(t)
    if result.skipped_hidden_bursts:
        console.print(f"  [dim]skipped {result.skipped_hidden_bursts:,} non-pick burst frame(s)[/dim]")

    if missing:
        console.print(
            f"\n[bold]missing:[/bold] {by_kind['photo']:,} photo(s), "
            f"{by_kind['video']:,} video(s), ~{missing_bytes / 1e9:.1f} GB"
        )
        mt = Table(show_header=True, header_style="bold", title="by capture month")
        mt.add_column("month")
        mt.add_column("assets", justify="right")
        for m, n in sorted(by_month.items()):
            mt.add_row(m, f"{n:,}")
        console.print(mt)
        console.print(
            "  cameras: " + ", ".join(f"{c} {n:,}" for c, n in by_camera.most_common(6))
        )
    console.print(f"\n[green]✓[/green] {len(missing):,} UUID(s) → [cyan]{out}[/cyan]")


@photos_app.command("pull")
def photos_pull(
    uuids_file: Path = typer.Option(
        Path.home() / ".immy" / "photos-missing.txt", "--uuids",
        help="UUID list to queue (from `immy photos diff`). Already-queued UUIDs are kept as-is.",
    ),
    dest: str = typer.Option(
        "n5:/mnt/tank/media/staging/photos", "--dest",
        help="host:/path of the photos staging root on n5 (`.staging/` + `ready/` under it). "
        "Use n5-lan / n5-tb4 at home.",
    ),
    batch_size: int = typer.Option(500, "--batch-size", help="Assets per batch (~7 GB at 500)."),
    max_batches: int = typer.Option(
        0, "--max-batches", help="Stop after this many NEW batches (0 = until the queue is empty).",
    ),
    export_root: Path = typer.Option(
        Path.home() / ".immy" / "photos-export", "--export-root",
        help="Local scratch for batch exports; a batch is deleted once n5 has it.",
    ),
    ledger_path: Path = typer.Option(
        Path.home() / ".immy" / "photos-pull.sqlite", "--ledger",
        help="Per-UUID delivery ledger (what makes failed transfers retry).",
    ),
    photos_library: Path = typer.Option(
        Path.home() / "Pictures" / "Photos Library.photoslibrary",
        "--photos-db",
        help="Path to a `.photoslibrary` bundle, or directly to Photos.sqlite.",
    ),
    keep_local: bool = typer.Option(False, "--keep-local", help="Keep batch exports after delivery."),
    delivery_retries: int = typer.Option(
        12, "--delivery-retries",
        help="Re-try a failed transfer this many times before giving up the run.",
    ),
    retry_wait: int = typer.Option(
        300, "--retry-wait", help="Seconds between transfer retries (12 × 300 s rides out a 1 h outage).",
    ),
    photos_app_fallback: bool = typer.Option(
        True, "--photos-app-fallback/--no-photos-app-fallback",
        help="Re-export assets osxphotos left incomplete via Photos.app AppleScript "
        "(fetches iCloud-only originals PhotoKit reports missing, e.g. a Live Photo's video).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the queue; export and send nothing."),
) -> None:
    """Export queued Photos.app assets and deliver them to n5 as batches.

    Each batch: `osxphotos export --uuid-from-file` (Photos.app downloads
    iCloud-only originals) → re-export anything incomplete through Photos.app
    AppleScript `export … with using originals` → drop any asset that still
    didn't arrive whole (e.g. a Live Photo missing its video) → rsync to `<dest>/.staging/<batch>` →
    verify → `mv` to `<dest>/ready/<batch>`. Batches that were exported but
    not delivered are re-sent first on the next run. See `immy.photos_pull`.
    """
    import shutil
    import time
    from . import photos_diff as pd
    from . import photos_pull as pp

    try:
        remote = pp.Remote.parse(dest)
        db_path = apple_photos_mod.resolve_db_path(photos_library)
    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)
    if shutil.which("osxphotos") is None:
        console.print("[red]osxphotos not on PATH[/red] — `uv tool install --python 3.12 'osxphotos==0.77.2'`")
        raise typer.Exit(code=2)
    fallback_run = None
    if photos_app_fallback:
        if shutil.which("osascript"):
            fallback_run = pp._run
        else:
            console.print("[yellow]osascript not on PATH — no Photos.app fallback[/yellow]")

    conn = pp.open_ledger(ledger_path)
    if uuids_file.exists():
        added = pp.enqueue(conn, uuids_file.read_text().split())
        if added:
            console.print(f"queued {added:,} new UUID(s) from [cyan]{uuids_file}[/cyan]")
    export_root = export_root.expanduser().resolve()

    def show_counts() -> None:
        c = pp.counts(conn)
        console.print("  ledger: " + " · ".join(
            f"{k} {c.get(k, 0):,}" for k in (pp.PENDING, pp.EXPORTED, pp.DELIVERED, pp.FAILED)
        ))

    console.print(f"[bold]photos pull[/bold] → [cyan]{remote.host}:{remote.root}[/cyan]")
    show_counts()
    resend = pp.undelivered_batches(conn)
    if dry_run:
        nxt = pp.next_uuids(conn, 10**9)
        console.print(f"  would re-send {len(resend)} batch(es), then export {len(nxt):,} "
                      f"asset(s) in batches of {batch_size}")
        for u, why in conn.execute(
            "SELECT uuid, last_error FROM uuid_state WHERE status=? LIMIT 10", (pp.FAILED,)
        ):
            console.print(f"  [yellow]failed[/yellow] {u}: {why}")
        return

    def send(batch: str) -> bool:
        batch_dir = export_root / batch
        if not batch_dir.is_dir():
            # Local export gone before n5 had it: re-queue its assets.
            conn.execute("UPDATE uuid_state SET status=?, batch=NULL WHERE batch=?",
                         (pp.PENDING, batch))
            conn.execute("DELETE FROM batch WHERE id=?", (batch,))
            conn.commit()
            console.print(f"  [yellow]{batch}: local export missing — re-queued[/yellow]")
            return True
        for attempt in range(delivery_retries + 1):
            try:
                pp.deliver(batch_dir, remote)
                break
            except pp.DeliveryError as e:
                if attempt == delivery_retries:
                    console.print(f"  [red]{batch}: {e}[/red]\n  (kept locally; re-sent on the next run)")
                    return False
                console.print(f"  [yellow]{batch}: {e} — retry {attempt + 1}/{delivery_retries} "
                              f"in {retry_wait}s[/yellow]")
                time.sleep(retry_wait)
        pp.mark_delivered(conn, batch)
        if not keep_local:
            shutil.rmtree(batch_dir)
        console.print(f"  [green]✓[/green] {batch} → ready/")
        return True

    for batch in resend:
        if not send(batch):
            raise typer.Exit(code=1)

    photos = pd.open_live_ro(db_path)
    made = 0
    try:
        while not max_batches or made < max_batches:
            uuids = pp.next_uuids(conn, batch_size)
            if not uuids:
                break
            console.print(f"  exporting {len(uuids):,} asset(s)…")
            batch, check = pp.export_batch(conn, photos, uuids, export_root,
                                           fallback_run=fallback_run)
            made += 1
            if check.rescued:
                console.print(f"    {len(check.rescued):,} asset(s) completed via Photos.app")
            for u, why in list(check.incomplete.items())[:5]:
                console.print(f"    [yellow]incomplete[/yellow] {u}: {why}")
            if len(check.incomplete) > 5:
                console.print(f"    [yellow]… {len(check.incomplete) - 5} more incomplete[/yellow]")
            if batch is None:
                console.print("    [yellow]nothing arrived whole — no batch[/yellow]")
                continue
            files, size = conn.execute(
                "SELECT files, bytes FROM batch WHERE id=?", (batch,)).fetchone()
            console.print(f"    {batch}: {len(check.complete):,} asset(s), "
                          f"{files:,} file(s), {size / 1e9:.1f} GB")
            if not send(batch):
                raise typer.Exit(code=1)
    finally:
        photos.close()
    show_counts()
    console.print(
        "\n[dim]on n5: deploy/n5/photos-ingest.sh (dedup report), "
        "then photos-ingest.sh --promote[/dim]"
    )


app.add_typer(photos_app, name="photos")


if __name__ == "__main__":
    app()
