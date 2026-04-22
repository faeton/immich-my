from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import bloat as bloat_mod
from . import captions as captions_mod
from . import clip as clip_mod
from . import offline as offline_mod
from . import process as process_mod
from . import promote as promote_mod
from . import pg as pg_mod
from . import transcripts as transcripts_mod
from .config import load as load_config
from .exif import has_gps, read_folder
from .immich import ImmichClient
from .notes import (
    ensure_notes,
    parse_frontmatter,
    resolve as resolve_notes,
    update_frontmatter,
)
from .rules import Finding, evaluate
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


def _first_present(row, *keys: str) -> tuple[object | None, str | None]:
    for key in keys:
        value = row.get(key)
        if value is not None:
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
    make = make or ""
    model = model or ""
    s = f"{make} {model}".strip()
    if s:
        return s

    hier = row.get("XMP:HierarchicalSubject")
    if isinstance(hier, list):
        for item in hier:
            if isinstance(item, str) and item.startswith("Gear/Camera/"):
                camera = item.removeprefix("Gear/Camera/").strip()
                if camera:
                    return f"{camera} (xmp)"

    subj = row.get("XMP:Subject")
    if isinstance(subj, list):
        for item in subj:
            if isinstance(item, str) and item.strip():
                text = item.strip()
                # Avoid generic trip/source tags; keep this conservative.
                if text not in {"IMG", "VID"} and "/" not in text:
                    return f"{text} (xmp)"

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


def _promote_impl(
    folder: Path,
    dry_run: bool,
    force: bool,
    config_path: Path | None,
) -> None:
    """Rsync + Immich library-scan + Insta360 stack calls.

    Shared body for the `promote` / `push` / `pub` aliases.
    """
    config = load_config(config_path)
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

    client: ImmichClient | None = None
    if config.immich is not None and not dry_run:
        client = ImmichClient(url=config.immich.url, api_key=config.immich.api_key)
    elif config.immich is None:
        console.print("[dim]no immich creds — rsync only, no scan or stacks.[/dim]")

    summary = promote_mod.execute(plan, config, dry_run=dry_run, client=client)

    prefix = "[yellow]dry-run[/yellow] " if dry_run else ""
    changed = len(summary["rsync_changes"])
    console.print(f"{prefix}rsync: {changed} change(s) to {summary['target']}")
    off = summary.get("offline_sync")
    if off:
        if "error" in off:
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
            colour = "green" if off["failed"] == 0 else "yellow"
            console.print(
                f"[{colour}]offline-sync:[/{colour}] synced {off['synced']} of "
                f"{off['pending']} pending"
                + (f", [red]{off['failed']} failed[/red]" if off["failed"] else "")
            )
    if "scan_error" in summary:
        console.print(f"[red]scan failed:[/red] {summary['scan_error']}")
    elif summary["scan_triggered"]:
        console.print("[green]✓[/green] library scan triggered")
    elif summary.get("scan_skipped_reason") == "y_processed":
        console.print("[dim]scan skipped: y_processed.yml present[/dim]")
        derivs = summary.get("derivatives") or {}
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
        colour = {
            "stacked": "green", "planned": "yellow", "skipped": "dim", "error": "red",
        }.get(status, "")
        console.print(f"  [{colour}]{status}[/{colour}] {detail}")
    album = summary.get("album") or {}
    if album and album.get("status") != "skipped":
        colour = {
            "created": "green", "updated": "green", "error": "red",
        }.get(album.get("status", ""), "")
        console.print(
            f"album [{colour}]{album['status']}[/{colour}] "
            f"{album['name']}: {album['detail']}"
            + (f" [dim]({album['missing']} asset(s) not yet indexed)[/dim]"
               if album.get("missing") else "")
        )


def _promote(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the plan; no rsync, no API calls."),
    force: bool = typer.Option(False, "--force", help="Promote even if HIGH findings are still pending."),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Rsync trip into originals + trigger Immich scan + stack Insta360 pairs."""
    _promote_impl(folder, dry_run=dry_run, force=force, config_path=config_path)


# Register under three names — Typer has no native aliases, so we just
# attach the same callback to each command name.
for _name in ("promote", "push", "pub"):
    app.command(name=_name)(_promote)


# --- `immy bloat` subcommands ---------------------------------------------

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


app.add_typer(bloat_app, name="bloat")


# --- `immy process` (Phase Y.1) -------------------------------------------


@app.command()
def process(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
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
    transcode_videos: bool = typer.Option(
        True, "--transcode/--no-transcode",
        help="Y.5 — emit a web-playable mp4 (libx264 720p, CRF 23) when the source isn't already h264/aac/mp4 ≤720p. Off → source plays only if the browser supports it.",
    ),
    offline: bool = typer.Option(
        False, "--offline",
        help="Skip Postgres; cache asset + embedding + caption data to .audit/offline/. Run `immy sync-offline <trip>` later to push. Requires one prior online run to have cached library info to ~/.immy/library.yml.",
    ),
    config_path: Path = typer.Option(None, "--config", help="Path to immy config (default: ~/.immy/config.yml)."),
) -> None:
    """Phase Y.1/Y.2 — insert asset + asset_exif rows for every media file
    under <folder> directly into the Immich Postgres, and optionally stage
    thumbnail + preview derivatives for `immy promote` to upload.

    Requires `pg:` and `immich.library_id` in ~/.immy/config.yml.
    `--with-derivatives` (default) additionally requires `media:` — pyvips
    writes webp/jpeg under `.audit/derivatives/thumbs/<userId>/...`.
    Idempotent via `checksum = sha1("path:" + originalPath)`.
    Drops `.audit/y_processed.yml` so `immy promote` skips the scan POST.
    """
    config = load_config(config_path)
    if config.immich is None:
        console.print(
            "[red]no immich: block in immy config[/red] — process needs "
            "`immich.library_id` to pick which library to write into."
        )
        raise typer.Exit(code=2)

    conn = None
    sink: offline_mod.Sink
    if offline:
        library = offline_mod.load_cached_library()
        recovered_from_marker = False
        if library is None:
            # Fallback: recover container_root from an existing marker
            # (this trip's own .audit/y_processed.yml, or any sibling).
            # owner_id/library_id stay as placeholders — sync-offline
            # resolves them from the live DB at push time.
            root = offline_mod.derive_container_root_from_marker(folder)
            if root is None and folder.parent.is_dir():
                derived = offline_mod.derive_library_from_any_trip(folder.parent)
                if derived is not None:
                    library = derived
                    recovered_from_marker = True
            elif root is not None:
                from .pg import LibraryInfo as _LI
                library = _LI(
                    id="__offline_placeholder__",
                    owner_id="__offline_placeholder__",
                    container_root=root,
                )
                recovered_from_marker = True
        if library is None:
            console.print(
                "[red]--offline needs library info[/red] and none was found. "
                f"Looked at {offline_mod.LIBRARY_CACHE_PATH} and "
                f"{folder}/.audit/y_processed.yml. Run `immy process` once "
                "online so library info gets cached."
            )
            raise typer.Exit(code=2)
        sink = offline_mod.OfflineSink(folder, library)
        hint = ""
        if recovered_from_marker:
            hint = " [dim](owner_id/library_id pulled at sync time)[/dim]"
        console.print(
            f"[yellow]offline mode[/yellow] — writes go to "
            f".audit/{offline_mod.OFFLINE_DIR_NAME}/{hint}\n"
            f"  container_root: {library.container_root}\n"
            f"  [dim]run `immy sync-offline {folder.name}` later to push[/dim]"
        )
    else:
        if config.pg is None:
            console.print(
                "[red]no pg: block in immy config[/red] — add "
                "host/port/user/password/database to ~/.immy/config.yml, "
                "or run with --offline."
            )
            raise typer.Exit(code=2)
        try:
            conn = pg_mod.connect(config.pg)
        except Exception as e:  # psycopg.OperationalError etc.
            console.print(
                f"[red]pg connect failed:[/red] {e}\n"
                f"[yellow]hint:[/yellow] if tailnet/NAS is unreachable, rerun "
                "with [bold]--offline[/bold] to cache work locally; sync later."
            )
            raise typer.Exit(code=2)
        try:
            library = pg_mod.fetch_library_info(conn, config.immich.library_id)
        except LookupError as e:
            console.print(f"[red]{e}[/red]")
            conn.close()
            raise typer.Exit(code=2)
        # Cache library info so a future --offline run has what it needs.
        offline_mod.cache_library_info(library)
        sink = offline_mod.PgSink(conn)

    console.print(
        f"[bold]process[/bold] {folder}\n"
        f"  library: {library.id} owner={library.owner_id}\n"
        f"  container root: {library.container_root}  "
        f"[dim](where files will live inside the Immich container after `immy promote`)[/dim]\n"
        f"  target prefix:  {library.container_root}/{folder.name}/...  "
        f"[dim](baked into asset.originalPath; no copy happens now)[/dim]\n"
        f"  [dim]nothing is copied — derivatives stage under .audit/; "
        f"rsync to NAS happens in `immy promote`.[/dim]"
    )

    if dry_run:
        from .exif import read_folder as _read
        rows = _read(folder)
        console.print(f"[yellow]dry-run[/yellow] would process {len(rows)} file(s)")
        for r in rows[:5]:
            asset, _ = process_mod.build_rows(r.path, folder, r, library)
            console.print(
                f"  {asset.asset_type:<5} {asset.original_path} "
                f"[dim]cs={asset.checksum.hex()[:12]}…[/dim]"
            )
        if len(rows) > 5:
            console.print(f"  [dim]… and {len(rows) - 5} more[/dim]")
        if conn is not None:
            conn.close()
        return

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
            "[yellow]note:[/yellow] --with-clip needs derivatives (CLIP runs "
            "on the preview file). Skipping CLIP this run."
        )
    compute_faces = with_faces and compute
    if with_faces and not compute:
        console.print(
            "[yellow]note:[/yellow] --with-faces needs derivatives (faces run "
            "on the preview file). Skipping faces this run."
        )
    clip_model = (
        config.ml.clip_model if config.ml is not None else clip_mod.DEFAULT_MODEL
    )
    transcript_model = transcripts_mod.DEFAULT_MODEL
    if config.ml is not None and config.ml.whisper_model:
        transcript_model = config.ml.whisper_model
    # Env var wins over config.yml so you can override per-run without
    # editing the file (handy on trips where the language mix changes).
    transcript_prompt = os.environ.get("IMMY_WHISPER_PROMPT") or (
        config.ml.whisper_prompt if config.ml is not None else None
    )
    # Resolve captioner config. Env vars shadow config.yml values so the
    # same `~/.immy/config.yml` works across backends without edits.
    captioner_config: captions_mod.CaptionerConfig | None = None
    if with_captions:
        ml = config.ml
        endpoint = (
            os.environ.get("IMMY_CAPTIONER_ENDPOINT")
            or (ml.captioner_endpoint if ml else None)
            or captions_mod.DEFAULT_ENDPOINT
        )
        model = (
            os.environ.get("IMMY_CAPTIONER_MODEL")
            or (ml.captioner_model if ml else None)
            or captions_mod.DEFAULT_MODEL
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
        phases.append(
            f"captions({captioner_config.model if captioner_config else '?'})"
        )
    console.print(
        f"  phases: {', '.join(phases) if phases else '[dim](EXIF + insert only)[/dim]'}"
    )

    # Rich's markup would turn filenames/prompts into junk; plain print
    # keeps the progress stream grep-friendly and tees cleanly to the
    # batch-run log. `highlight=False` suppresses rich's auto-coloring.
    def _progress(msg: str) -> None:
        console.print(msg, highlight=False)

    try:
        results = process_mod.process_trip(
            folder, conn, library,
            sink=sink,
            compute_derivatives=compute,
            compute_clip=compute_clip,
            compute_faces=compute_faces,
            compute_transcripts=with_transcripts,
            compute_captions=with_captions,
            captioner_config=captioner_config,
            transcode_videos=transcode_videos,
            clip_model=clip_model,
            transcript_model=transcript_model,
            transcript_prompt=transcript_prompt,
            progress=_progress,
        )
        sink.commit()
    except Exception as e:
        sink.rollback()
        console.print(f"[red]process failed, rolled back:[/red] {e}")
        raise typer.Exit(code=1)
    finally:
        sink.close()

    new_count = sum(1 for r in results if r.inserted)
    existed = len(results) - new_count
    derivs = sum(len(r.derivatives) for r in results if r.derivatives)
    clipped = sum(1 for r in results if r.clip_embedded)
    face_count = sum(r.faces_detected for r in results)
    transcript_count = sum(1 for r in results if r.transcript)
    caption_count = sum(1 for r in results if r.caption)
    process_mod.write_marker(folder, results)
    tail = f", [cyan]{derivs} derivative file(s) staged[/cyan]" if derivs else ""
    tail += f", [cyan]{clipped} CLIP embedding(s)[/cyan]" if clipped else ""
    tail += f", [cyan]{face_count} face(s)[/cyan]" if face_count else ""
    tail += f", [cyan]{transcript_count} transcript(s)[/cyan]" if transcript_count else ""
    tail += f", [cyan]{caption_count} caption(s)[/cyan]" if caption_count else ""
    console.print(
        f"[green]✓[/green] {new_count} new asset(s), "
        f"[dim]{existed} already present[/dim]{tail}  "
        f"(marker: .audit/{process_mod.Y_MARKER_FILENAME})"
    )


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

    entries = list(offline_mod.iter_entries(folder))
    if not entries:
        console.print(
            f"[dim]no offline entries under {folder}/.audit/"
            f"{offline_mod.OFFLINE_DIR_NAME}/ — nothing to sync.[/dim]"
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
        console.print(msg, highlight=False)

    try:
        summary = offline_mod.sync_trip(
            folder, conn, library=library, progress=_progress,
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
    if summary["failed"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
