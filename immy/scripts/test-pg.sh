#!/bin/sh
# A throwaway Postgres for the SQL-level tests (tests/test_sql_pg.py): the
# same image Immich runs, on tmpfs, bound to localhost. Never the live
# Immich database; the suite refuses to touch that (tests/conftest.py).
#
#   scripts/test-pg.sh up      # start it, print the DSN to export
#   IMMY_TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/postgres \
#     uv run --no-sync pytest tests/test_sql_pg.py
#   scripts/test-pg.sh down
set -eu
NAME=immy-test-pg
PORT=${IMMY_TEST_PG_PORT:-55432}
IMAGE=${IMMY_TEST_PG_IMAGE:-$(sudo docker inspect immich_postgres --format '{{.Config.Image}}' 2>/dev/null || echo postgres:14)}
case "${1:-up}" in
  up)
    sudo docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=test \
      -p 127.0.0.1:"$PORT":5432 --tmpfs /var/lib/postgresql/data "$IMAGE" >/dev/null
    until sudo docker exec "$NAME" pg_isready -U postgres -q 2>/dev/null; do sleep 1; done
    echo "export IMMY_TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:$PORT/postgres" ;;
  down) sudo docker rm -f "$NAME" >/dev/null ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
