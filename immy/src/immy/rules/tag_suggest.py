"""Auto-propose MEDIUM tag additions for hand-edited notes.

The scaffold-on-first-audit path seeds a sensible `tags:` block, but
once the user has edited the notes file we never touch it again — by
design. That means a user who scaffolded, deleted the camera tag to
clean up, then added a new camera later loses our auto-Gear/Camera
suggestion even though it would be obvious from EXIF.

This rule runs every audit, compares the current notes `tags:` list
to what the scaffold *would* produce from the current folder contents,
and proposes any missing tag in a new "category" the user hasn't
already populated. Category = everything before the last `/`:

  Events/Mau-Lions-1           → category "Events"
  Gear/Camera/Nikon Z50_2      → category "Gear/Camera"
  Source/Nikon                 → category "Source"

If the user already has *any* tag with the same category we skip the
suggestion — they customized on purpose. Only empty categories trigger
a proposal. This makes the rule quiet on well-curated folders and
chatty only when something actually-obvious is missing.

Opt-out: set `tag_suggestions: off` in notes front-matter.

MEDIUM tier → user sees a y/n prompt with the list, or blanket-accepts
under `--yes-medium`. Accepted additions write to notes front-matter via
the `write_notes` action; the next apply pass lets `trip-tags-from-notes`
cascade them into XMP.
"""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from ..notes import detect_identity, parse_frontmatter, resolve, suggested_tags
from .registry import Finding, Rule, register


def _category(tag: str) -> str:
    parts = tag.rsplit("/", 1)
    return parts[0] if len(parts) == 2 else tag


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    existing = fm.get("tags")
    if not isinstance(existing, list):
        return []  # first-run scaffold seeds tags; nothing to diff yet
    opt = fm.get("tag_suggestions", True)
    if opt is False or str(opt).lower() in ("off", "no", "false", "disabled"):
        return []
    identity = detect_identity(folder, rows)
    proposed = suggested_tags(identity)
    existing_set = set(existing)
    existing_cats = {_category(t) for t in existing if isinstance(t, str)}
    missing = [
        t for t in proposed
        if t not in existing_set and _category(t) not in existing_cats
    ]
    if not missing:
        return []
    reason = f"notes `tags:` missing obvious: {', '.join(missing)}"
    return [Finding(
        rule="tag-suggest-missing",
        confidence="medium",
        path=notes,
        action="write_notes",
        patch={"add_tags": missing},
        reason=reason,
    )]


register(Rule(name="tag-suggest-missing", confidence="medium", propose=_propose))
