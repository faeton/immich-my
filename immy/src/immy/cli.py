from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import bloat as bloat_mod
from . import clip as clip_mod
from . import process as process_mod
from . import promote as promote_mod
from . import pg as pg_mod
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
    transcode_videos: bool = typer.Option(
        True, "--transcode/--no-transcode",
        help="Y.5 — emit a web-playable mp4 (libx264 720p, CRF 23) when the source isn't already h264/aac/mp4 ≤720p. Off → source plays only if the browser supports it.",
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
    if config.pg is None:
        console.print(
            "[red]no pg: block in immy config[/red] — add host/port/user/password/database "
            "to ~/.immy/config.yml."
        )
        raise typer.Exit(code=2)
    if config.immich is None:
        console.print(
            "[red]no immich: block in immy config[/red] — process needs "
            "`immich.library_id` to pick which library to write into."
        )
        raise typer.Exit(code=2)

    try:
        conn = pg_mod.connect(config.pg)
    except Exception as e:  # psycopg.OperationalError etc.
        console.print(f"[red]pg connect failed:[/red] {e}")
        raise typer.Exit(code=2)

    try:
        library = pg_mod.fetch_library_info(conn, config.immich.library_id)
    except LookupError as e:
        console.print(f"[red]{e}[/red]")
        conn.close()
        raise typer.Exit(code=2)

    console.print(
        f"[bold]process[/bold] {folder}\n"
        f"  library: {library.id} owner={library.owner_id}\n"
        f"  container root: {library.container_root}\n"
        f"  target prefix: {library.container_root}/{folder.name}/..."
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
    try:
        results = process_mod.process_trip(
            folder, conn, library,
            compute_derivatives=compute,
            compute_clip=compute_clip,
            compute_faces=compute_faces,
            transcode_videos=transcode_videos,
            clip_model=clip_model,
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        console.print(f"[red]insert failed, rolled back:[/red] {e}")
        raise typer.Exit(code=1)
    finally:
        if not conn.closed:
            conn.close()

    new_count = sum(1 for r in results if r.inserted)
    existed = len(results) - new_count
    derivs = sum(len(r.derivatives) for r in results if r.derivatives)
    clipped = sum(1 for r in results if r.clip_embedded)
    face_count = sum(r.faces_detected for r in results)
    process_mod.write_marker(folder, results)
    tail = f", [cyan]{derivs} derivative file(s) staged[/cyan]" if derivs else ""
    tail += f", [cyan]{clipped} CLIP embedding(s)[/cyan]" if clipped else ""
    tail += f", [cyan]{face_count} face(s)[/cyan]" if face_count else ""
    console.print(
        f"[green]✓[/green] {new_count} new asset(s), "
        f"[dim]{existed} already present[/dim]{tail}  "
        f"(marker: .audit/{process_mod.Y_MARKER_FILENAME})"
    )


if __name__ == "__main__":
    app()
