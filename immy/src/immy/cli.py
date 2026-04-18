from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .exif import has_gps, read_folder
from .notes import (
    ensure_notes,
    parse_frontmatter,
    resolve as resolve_notes,
    update_frontmatter,
)
from .rules import Finding, evaluate
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
    """Apply a note-edit patch. Today supports `add_tags` (append unique,
    preserve order). More keys can join here as write_notes rules grow."""
    add_tags = f.patch.get("add_tags") or []
    if not add_tags:
        return
    fm = parse_frontmatter(f.path)
    existing = fm.get("tags")
    merged: list = list(existing) if isinstance(existing, list) else []
    seen = set(merged)
    for t in add_tags:
        if t not in seen:
            merged.append(t)
            seen.add(t)
    update_frontmatter(f.path, {"tags": merged})


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


def _dedup_by_field(findings: list[Finding]) -> list[Finding]:
    """Per-tier, per-(path, xmp_field) dedup. Within a confidence tier the
    first-registered rule wins (specific > general). Tiers are independent
    so a MEDIUM finding still surfaces when a HIGH rule claims the same
    field — the user decides whether MEDIUM overrides after HIGH lands."""
    out: list[Finding] = []
    for tier in ("high", "medium", "low"):
        claimed: set[tuple] = set()
        for f in findings:
            if f.confidence != tier:
                continue
            if f.action != "write_xmp":
                out.append(f)
                continue
            remaining = {k: v for k, v in f.patch.items() if (f.path, k) not in claimed}
            if not remaining:
                continue
            for k in remaining:
                claimed.add((f.path, k))
            out.append(f if remaining == f.patch else replace(f, patch=remaining))
    return out


def _fmt_date(row) -> str:
    v = row.get(
        "XMP:DateTimeOriginal", "EXIF:DateTimeOriginal",
        "QuickTime:CreateDate", "EXIF:CreateDate",
    )
    return str(v) if v else "—"


def _fmt_gps(row) -> str:
    lat = row.get("Composite:GPSLatitude", "EXIF:GPSLatitude")
    lon = row.get("Composite:GPSLongitude", "EXIF:GPSLongitude")
    if lat is None or lon is None:
        return "—"
    return f"{float(lat):+.4f},{float(lon):+.4f}"


def _fmt_make_model(row) -> str:
    make = row.get("EXIF:Make", "QuickTime:Make") or ""
    model = row.get("EXIF:Model", "QuickTime:Model") or ""
    s = f"{make} {model}".strip()
    return s or "—"


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
    - interactive → y/n/skip per finding
    - else → accept none (they stay pending for a later run)
    """
    if not findings:
        return []
    if yes_medium:
        return list(findings)
    if not interactive:
        return []
    accepted: list[Finding] = []
    console.print(f"\n[bold]{len(findings)} MEDIUM finding(s) need review[/bold]")
    for f in findings:
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


@app.command()
def promote(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
) -> None:
    """Rsync + Immich library-scan. Stub until 2a.4."""
    typer.echo(f"[promote] not implemented yet (iteration 2a.4). target: {folder}")
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
