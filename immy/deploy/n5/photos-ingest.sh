#!/bin/sh
# Photos-bridge ingest on the N5: dedup the batches `immy photos pull` (on the
# Mac) delivered to staging/photos/ready/, against the live manifest.
#
# Usage:
#   ./photos-ingest.sh             # register → fingerprint → cluster → confirm → decide
#                                  # (moves NO files; ends with a report)
#   ./photos-ingest.sh --promote   # same, then promote-rest --write into the library
#
# Default stops at `decide` on purpose: read the report (new auto/review clusters
# touching photos rows) before anything lands in /originals. After --promote,
# queue an IMMY-Sync library scan; scanned assets get CLIP + faces automatically.
set -eu

COMPOSE="${IMMY_COMPOSE:-/mnt/flash/immy/src-immy/deploy/n5/compose.yaml}"
DB=/mnt/tank/media/state/manifest.sqlite
M="--manifest /state/manifest.sqlite"
LOG="/mnt/tank/media/state/logs/photos-ingest-$(date +%Y%m%d-%H%M%S).log"

immy() { sudo -n docker compose -f "$COMPOSE" run --rm -T immy "$@"; }

{
  echo "== photos-ingest $(date -Is)"
  echo "== register";    immy dedup register photos /staging/photos/ready $M
  echo "== fingerprint"; immy dedup fingerprint $M --source photos
  echo "== cluster";     immy dedup cluster $M
  echo "== confirm";     immy dedup confirm $M
  echo "== decide";      immy dedup decide $M

  echo "== photos rows by status / component / date source"
  sqlite3 -readonly "$DB" "SELECT status, component, taken_src, count(*) FROM asset
                           WHERE source='photos' GROUP BY 1,2,3"
  echo "== clusters touching photos rows (decision, members)"
  sqlite3 -readonly "$DB" "SELECT c.decision, count(DISTINCT c.id), count(*) FROM cluster c
                           JOIN membership m ON m.cluster_id = c.id
                           JOIN asset a ON a.id = m.asset_id
                           WHERE c.id IN (SELECT m2.cluster_id FROM membership m2
                                          JOIN asset a2 ON a2.id = m2.asset_id
                                          WHERE a2.source = 'photos')
                           GROUP BY 1"
  echo "== errors"
  sqlite3 -readonly "$DB" "SELECT path, error FROM asset WHERE source='photos' AND status='error' LIMIT 20"

  if [ "${1:-}" = "--promote" ]; then
    echo "== promote-rest"
    immy dedup promote-rest $M --originals /originals --write
  else
    echo "== stopped before promote (dry-run below); re-run with --promote"
    immy dedup promote-rest $M --originals /originals
  fi
} 2>&1 | grep -v '^\s*$' | tee "$LOG"
