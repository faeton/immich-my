"""Unit tests for `notes.notes_body` — what goes into the Immich album description.

The helper strips the front-matter, a leading `# Title` heading, and the
scaffold-hint italic paragraph `immy audit` writes on first run. What
remains is the user's own trip prose, ready to be the album description.
"""

from __future__ import annotations

from pathlib import Path

from immy.notes import notes_body


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_notes_body_strips_frontmatter_and_title(tmp_path: Path):
    notes = _write(tmp_path / "README.md", (
        "---\n"
        "trip: safari\n"
        "---\n"
        "\n"
        "# safari\n"
        "\n"
        "Lions at dawn. Giraffes blocked the north track.\n"
    ))
    assert notes_body(notes) == "Lions at dawn. Giraffes blocked the north track."


def test_notes_body_strips_scaffold_hint(tmp_path: Path):
    # Exact shape of the scaffold text from `ensure_notes`.
    notes = _write(tmp_path / "README.md", (
        "---\n"
        "trip: safari\n"
        "---\n"
        "\n"
        "# safari\n"
        "\n"
        "_Scaffold by `immy audit`. Fill `location` (either `name:` or\n"
        "`coords: [lat, lon]`). Edit `tags:` to taste. Front-matter above\n"
        "drives XMP writes on the next `--write`._\n"
    ))
    assert notes_body(notes) == ""


def test_notes_body_keeps_user_content_after_scaffold_hint(tmp_path: Path):
    notes = _write(tmp_path / "README.md", (
        "---\n"
        "trip: safari\n"
        "---\n"
        "\n"
        "# safari\n"
        "\n"
        "_Scaffold by `immy audit`. Fill `location` (either `name:` or\n"
        "coords. Edit `tags:` to taste._\n"
        "\n"
        "Real prose here.\n"
    ))
    assert notes_body(notes) == "Real prose here."


def test_notes_body_no_frontmatter(tmp_path: Path):
    notes = _write(tmp_path / "README.md", "Just raw prose.\n")
    assert notes_body(notes) == "Just raw prose."


def test_notes_body_preserves_paragraph_structure(tmp_path: Path):
    notes = _write(tmp_path / "TRIP.md", (
        "---\n"
        "trip: trip\n"
        "---\n"
        "\n"
        "# trip\n"
        "\n"
        "First paragraph.\n"
        "Still first.\n"
        "\n"
        "Second paragraph.\n"
    ))
    out = notes_body(notes)
    assert "First paragraph.\nStill first." in out
    assert "Second paragraph." in out
    assert out.count("\n\n") == 1  # exactly one paragraph break


def test_notes_body_empty_body(tmp_path: Path):
    notes = _write(tmp_path / "README.md", "---\ntrip: x\n---\n")
    assert notes_body(notes) == ""
