"""A loser is quarantined only once its winner is safely in the library
(audit 2026-10, item 3).

`apply_decisions` used to walk `decided` rows by asset id. A loser with a
lower id than its winner was quarantined first, and if the winner's move
then failed, the cluster ended with no copy in the library and its only
other copy in quarantine — one purge away from gone. Now every cluster is
handled winner first, and a loser moves only when the winner is
`promoted` with a recorded dest_path whose bytes still hash to the recorded
sha256, or is the `canonical` library file itself.

The same rule gates quarantine purge (`purge_candidates`): a quarantined
row whose winner is not promoted/canonical is refused.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from immy.dedup import engine, manifest


def _file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _insert(conn, asset_id, path, status, *, source="icloud", dest_path=None,
            sha256=None, alias_path=None, nbytes=None):
    conn.execute(
        "INSERT INTO asset (id, source, path, status, bytes, taken_at, media_type,"
        " dest_path, sha256, alias_path)"
        " VALUES (?, ?, ?, ?, ?, '2024-06-15T10:00:00', 'image', ?, ?, ?)",
        (asset_id, source, str(path), status, nbytes, dest_path, sha256, alias_path),
    )


def _cluster(conn, winner, members, cluster_id=1):
    conn.execute("INSERT INTO cluster (id, decision, winner_asset_id) VALUES (?, 'auto', ?)",
                 (cluster_id, winner))
    for m in members:
        conn.execute("INSERT INTO membership (cluster_id, asset_id, role) VALUES (?, ?, ?)",
                     (cluster_id, m, "winner" if m == winner else "loser"))


def _status(conn, asset_id):
    return conn.execute("SELECT status FROM asset WHERE id=?", (asset_id,)).fetchone()[0]


def _apply(conn, tmp_path, *, dry_run=False, **kw):
    return engine.apply_decisions(
        conn, originals_root=tmp_path / "originals",
        quarantine_root=tmp_path / "quarantine", dry_run=dry_run, **kw,
    )


# ------------------------------------------------------------ apply gating


def test_loser_is_held_when_its_winner_fails_to_move(tmp_path):
    """The loser has the LOWER id — the old by-id walk quarantined it before
    ever attempting the winner, whose source is missing."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    loser = _file(tmp_path / "staging" / "b" / "IMG_1.JPG", b"loser bytes")
    _insert(conn, 1, loser, manifest.DECIDED, nbytes=11)
    _insert(conn, 2, tmp_path / "staging" / "a" / "missing.JPG", manifest.DECIDED, nbytes=11)
    _cluster(conn, 2, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["promoted"], result["quarantined"]) == (0, 0)
    assert result["errors"] == 1 and result["losers_held"] == 1
    assert any("winner" in s for s in result["held_samples"])
    assert loser.read_bytes() == b"loser bytes"
    assert _status(conn, 1) == manifest.DECIDED


def test_winner_goes_first_whatever_the_ids(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    loser = _file(tmp_path / "staging" / "b" / "IMG_1.JPG", b"loser")
    winner = _file(tmp_path / "staging" / "a" / "IMG_1.HEIC", b"winner")
    _insert(conn, 1, loser, manifest.DECIDED, nbytes=5)
    _insert(conn, 2, winner, manifest.DECIDED, nbytes=6)
    _cluster(conn, 2, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["promoted"], result["quarantined"], result["errors"]) == (1, 1, 0)
    assert result["losers_held"] == 0
    assert (_status(conn, 1), _status(conn, 2)) == (manifest.QUARANTINED, manifest.PROMOTED)


def test_loser_follows_a_winner_promoted_in_an_earlier_run(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"winner")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED,
            dest_path=str(lib), sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["quarantined"], result["losers_held"]) == (1, 0)
    assert not loser.exists()


def test_loser_is_held_when_the_promoted_winner_no_longer_verifies(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"changed!")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED,
            dest_path=str(lib), sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    for dry_run in (True, False):
        result = _apply(conn, tmp_path, dry_run=dry_run)
        assert (result["quarantined"], result["losers_held"]) == (0, 1)
    assert loser.exists() and _status(conn, 2) == manifest.DECIDED


def test_loser_is_held_when_the_promoted_winner_has_no_recorded_dest(tmp_path):
    """A pre-v4 `promoted` row records no dest_path: nothing to verify."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED)
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["quarantined"], result["losers_held"]) == (0, 1)
    assert loser.exists()


def test_canonical_winner_present_lets_the_loser_go(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"winner")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, lib, manifest.CANONICAL, source="originals", sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["quarantined"], result["losers_held"]) == (1, 0)


def test_canonical_winner_missing_holds_the_loser(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "originals" / "gone.HEIC", manifest.CANONICAL,
            source="originals")
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["quarantined"], result["losers_held"]) == (0, 1)
    assert loser.exists() and _status(conn, 2) == manifest.DECIDED


def test_a_held_loser_moves_once_the_winner_succeeds(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    loser = _file(tmp_path / "staging" / "b" / "IMG_1.JPG", b"loser")
    winner_path = tmp_path / "staging" / "a" / "IMG_1.HEIC"
    _insert(conn, 1, loser, manifest.DECIDED, nbytes=5)
    _insert(conn, 2, winner_path, manifest.DECIDED, nbytes=6)
    _cluster(conn, 2, (1, 2))
    conn.commit()
    assert _apply(conn, tmp_path)["losers_held"] == 1

    _file(winner_path, b"winner")                  # the source comes back
    result = _apply(conn, tmp_path)

    assert (result["promoted"], result["quarantined"], result["losers_held"]) == (1, 1, 0)


def test_legacy_winner_is_found_at_its_expected_path_and_recorded(tmp_path):
    """Promoted before schema v4 recorded dest_path: the expected library
    path is resolved, its bytes must hash to the recorded sha256, and only
    then is dest_path written back and the loser let go."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"winner")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    dry = _apply(conn, tmp_path, dry_run=True)
    assert (dry["quarantined"], dry["losers_held"]) == (1, 0)
    assert conn.execute("SELECT dest_path FROM asset WHERE id=1").fetchone()[0] is None

    result = _apply(conn, tmp_path)

    assert (result["quarantined"], result["losers_held"]) == (1, 0)
    assert conn.execute("SELECT dest_path FROM asset WHERE id=1").fetchone()[0] == str(lib)


def test_legacy_winner_at_its_collision_name_is_found_too(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"a stranger")
    lib = _file(tmp_path / "originals" / "2024" / "06" / "W__1.HEIC", b"winner")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    assert _apply(conn, tmp_path)["quarantined"] == 1
    assert conn.execute("SELECT dest_path FROM asset WHERE id=1").fetchone()[0] == str(lib)


def test_legacy_winner_is_held_when_both_candidate_paths_match(tmp_path):
    """Ambiguous: which file is this asset's? Not guessed."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"winner")
    _file(tmp_path / "originals" / "2024" / "06" / "W__1.HEIC", b"winner")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    assert _apply(conn, tmp_path)["losers_held"] == 1
    assert conn.execute("SELECT dest_path FROM asset WHERE id=1").fetchone()[0] is None


def test_legacy_winner_whose_expected_path_holds_other_bytes_is_not_recorded(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"a stranger")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, sha256=_sha(b"winner"))
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    result = _apply(conn, tmp_path)

    assert (result["quarantined"], result["losers_held"]) == (0, 1)
    assert conn.execute("SELECT dest_path FROM asset WHERE id=1").fetchone()[0] is None


def test_canonical_winner_without_any_recorded_hash_holds_the_loser(tmp_path):
    """Present is not proven: with no sha256 on the row or in library_file
    there is nothing to check the file against."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "W.HEIC", b"winner")
    loser = _file(tmp_path / "staging" / "L.JPG", b"loser")
    _insert(conn, 1, lib, manifest.CANONICAL, source="originals")
    _insert(conn, 2, loser, manifest.DECIDED, nbytes=5)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    assert _apply(conn, tmp_path)["losers_held"] == 1

    # `dedup index-library` records what the library holds; that hash counts.
    manifest.upsert_library_file(conn, lib, _sha(b"winner"), lib.stat())
    conn.commit()
    assert _apply(conn, tmp_path)["quarantined"] == 1


# ------------------------------------------------------------- purge gate


def test_purge_refuses_a_loser_whose_winner_never_reached_the_library(tmp_path):
    """Exactly the state the old by-id apply could leave behind."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    q = _file(tmp_path / "quarantine" / "L.JPG", b"loser")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.DECIDED)
    _insert(conn, 2, tmp_path / "staging" / "L.JPG", manifest.QUARANTINED, dest_path=str(q))
    _cluster(conn, 1, (1, 2))
    conn.commit()

    eligible, refused = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert eligible == []
    assert [asset_id for asset_id, _ in refused] == [2]


def test_purge_accepts_losers_of_promoted_and_canonical_winners(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "W.HEIC", b"w")
    canon = _file(tmp_path / "originals" / "C.HEIC", b"c")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, dest_path=str(lib),
            sha256=_sha(b"w"))
    _insert(conn, 2, tmp_path / "staging" / "L.JPG", manifest.QUARANTINED)
    _insert(conn, 3, canon, manifest.CANONICAL, source="originals", sha256=_sha(b"c"))
    _insert(conn, 4, tmp_path / "staging" / "L2.JPG", manifest.QUARANTINED)
    _cluster(conn, 1, (1, 2), cluster_id=1)
    _cluster(conn, 3, (3, 4), cluster_id=2)
    conn.commit()

    eligible, refused = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert (eligible, refused) == ([2, 4], [])


def test_purge_refuses_when_the_winner_file_is_gone(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _insert(conn, 1, tmp_path / "originals" / "gone.HEIC", manifest.CANONICAL,
            source="originals", sha256=_sha(b"c"))
    _insert(conn, 2, tmp_path / "staging" / "L.JPG", manifest.QUARANTINED)
    _insert(conn, 3, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED,
            dest_path=str(tmp_path / "originals" / "also-gone.HEIC"), sha256=_sha(b"w"))
    _insert(conn, 4, tmp_path / "staging" / "L2.JPG", manifest.QUARANTINED)
    _cluster(conn, 1, (1, 2), cluster_id=1)
    _cluster(conn, 3, (3, 4), cluster_id=2)
    conn.commit()

    eligible, refused = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert eligible == [] and [a for a, _ in refused] == [2, 4]


def test_purge_judges_an_alias_by_its_library_twin(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "A.JPG", b"same")
    swapped = _file(tmp_path / "originals" / "D.JPG", b"replaced")
    _insert(conn, 1, tmp_path / "staging" / "A.JPG", manifest.QUARANTINED, alias_path=str(lib),
            sha256=_sha(b"same"))
    _insert(conn, 2, tmp_path / "staging" / "B.JPG", manifest.QUARANTINED,
            alias_path=str(tmp_path / "originals" / "gone.JPG"), sha256=_sha(b"b"))
    _insert(conn, 3, tmp_path / "staging" / "C.JPG", manifest.QUARANTINED)  # no winner at all
    # The twin is there but no longer holds the alias's bytes.
    _insert(conn, 4, tmp_path / "staging" / "D.JPG", manifest.QUARANTINED,
            alias_path=str(swapped), sha256=_sha(b"original"))
    conn.commit()

    eligible, refused = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert eligible == [1] and [a for a, _ in refused] == [2, 3, 4]


def test_purge_refuses_a_loser_whose_promoted_winner_was_replaced(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "W.HEIC", b"something else now")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, dest_path=str(lib),
            sha256=_sha(b"winner"))
    _insert(conn, 2, tmp_path / "staging" / "L.JPG", manifest.QUARANTINED)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    eligible, refused = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert eligible == [] and [a for a, _ in refused] == [2]


def test_purge_refuses_a_legacy_winner_with_no_recorded_hash(tmp_path):
    """n5's 2026-07 apply predates sha256/dest_path: a file of the right name
    at the expected path proves nothing about which bytes it holds."""
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"winner")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED)
    _insert(conn, 2, tmp_path / "staging" / "L.JPG", manifest.QUARANTINED)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    eligible, refused = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert eligible == [] and "sha256" in refused[0][1]


def test_purge_accepts_a_legacy_winner_it_can_prove_and_records_it(tmp_path):
    conn = manifest.open_manifest(tmp_path / "m.sqlite")
    lib = _file(tmp_path / "originals" / "2024" / "06" / "W.HEIC", b"winner")
    _insert(conn, 1, tmp_path / "staging" / "W.HEIC", manifest.PROMOTED, sha256=_sha(b"winner"))
    _insert(conn, 2, tmp_path / "staging" / "L.JPG", manifest.QUARANTINED)
    _cluster(conn, 1, (1, 2))
    conn.commit()

    eligible, _ = engine.purge_candidates(conn, originals_root=tmp_path / "originals")

    assert eligible == [2]
    assert conn.execute("SELECT dest_path FROM asset WHERE id=1").fetchone()[0] == str(lib)
