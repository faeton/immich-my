# Deploy — Phase 0 Layout

Example self-hosted layout for this project. Public docs use `${...}`
placeholders from the repo-root `.env.example`; copy that file to `.env`
locally if you want shell snippets here to expand cleanly.

## Host

- Synology DS923+, DSM 7.2+, Container Manager installed.
- NVMe slots: **SSD read/write cache** (`md3`, RAID1) in front of `/volume1`.
  Not a separate storage pool. All Immich state lives on `/volume1` and gets
  the cache's benefit transparently.
- Access: **Tailscale-first**. The DS923+ runs the Synology Tailscale package
  (tailnet `${TAILNET_DOMAIN}`, host `${NAS_HOST}`). **Principle: every service keeps
  its native port; Tailscale Serve adds HTTPS in parallel on the same port.**
  DSM's landing on `:80`/`:443` is untouched. Canonical Immich URL:
  **`${IMMICH_URL}`** — valid Let's Encrypt cert
  auto-renewed by `tailscaled`. LAN access on `http://<lan-ip>:2283/` still
  works unchanged (tailscale serve intercepts on the tailnet IP only).
- User: `${DSM_USER}`. If the NAS already runs other containers, keep this
  project namespaced so nothing is shared unintentionally.

## Filesystem layout

Dedicated top-level share (not under the user's home dir, so DSM User Home
Service changes can't nuke it):

```
${DEPLOY_ROOT}/
├── docker/
│   ├── docker-compose.yml
│   ├── .env
│   ├── postgres/           # Postgres 16 data dir (chattr +C = no CoW on btrfs)
│   └── model-cache/        # immich-machine-learning model cache (bind mount)
├── library/                # UPLOAD_LOCATION — Immich-managed uploads
└── originals/              # external library root, mounted read-only into the server
```

Why a top-level share:
- Survives User Home Service being toggled.
- Survives ACL resets when someone's home is recreated.
- Clean to back up as a unit with Hyper Backup.

## Docker project

- Project name: **`${COMPOSE_PROJECT}`**.
- Container names: `immich_server`, `immich_postgres`, `immich_redis`,
  `immich_machine_learning`.
- Compose file: patched from the Immich release `docker-compose.yml`. Diffs vs
  upstream:
  - `name: immich` → `name: ${COMPOSE_PROJECT}`
  - `immich-server` volumes: added
    `- ${SHARED_ORIGINALS}:${EXTERNAL_ORIGINALS_MOUNT}:ro`
  - `immich-machine-learning` model cache: bind mount
    `- ${DOCKER_ROOT}/model-cache:/cache` (replaces the named
    volume) — keeps model blobs on the NAS share, not in the Docker root.
  - Top-level `volumes: model-cache:` block removed (unused after the bind).
  - `DB_STORAGE_TYPE: 'HDD'` left commented — /volume1 is SSD-cached btrfs.

### `.env` (do not commit secrets anywhere shared)

```env
UPLOAD_LOCATION=${SHARED_LIBRARY}
DB_DATA_LOCATION=${DOCKER_ROOT}/postgres
TZ=Europe/Lisbon
IMMICH_VERSION=release
DB_PASSWORD=***                   # regenerate if this file ever leaks
DB_USERNAME=postgres
DB_DATABASE_NAME=immich
```

Container-internal paths the rest of the plan references:
- `/data` — server's view of `UPLOAD_LOCATION`.
- `${EXTERNAL_ORIGINALS_MOUNT}` — read-only view of the host `originals/` share;
  this is the path to hand to **Admin → Libraries → External**.

## First-boot configuration (captured so we don't forget)

| Setting | Value | Why |
|---|---|---|
| Map | **on** | tiles come from OSM; the privacy trade (rough tile bbox leak) is acceptable for personal use. |
| Version check | **on** | we care about CVEs more than we care about a once-a-day ping. |
| Google Cast | **off** | loads Google's SDK into the web UI; we never cast. |
| Storage template | **on** |  |
| Template expression | `{{y}}/{{y}}-{{MM}}-{{dd}}/{{HH}}{{mm}}{{ss}}-{{filename}}` | accommodates iPhone + Fuji + drone + 360 producing same-name files on the same day. |

## Run commands (from the NAS)

```sh
cd "${DOCKER_ROOT}"

# full status
/usr/local/bin/docker compose ps

# follow logs (server is the usual one)
/usr/local/bin/docker compose logs -f immich-server

# restart one service
/usr/local/bin/docker compose restart immich-server

# pull new image + recreate (run AFTER reading the release notes)
/usr/local/bin/docker compose pull
/usr/local/bin/docker compose up -d
```

(Container Manager's UI shows the same project as `${COMPOSE_PROJECT}` and can start/stop
it, but the CLI is faster for logs.)

## Tailscale Serve (HTTPS on native ports)

Model: DSM keeps `:80` and `:443` for its own landing / Application Portal
(unchanged). We do **not** shadow 443. Instead, each container we want exposed
gets an HTTPS wrapper on its own port via Tailscale Serve.

Current serve config:

```sh
sudo /usr/local/bin/tailscale serve --bg --https=${IMMICH_PORT} http://127.0.0.1:${IMMICH_PORT}
```

Result over tailnet:
- `http://${NAS_HOST}.${TAILNET_DOMAIN}/` → DSM nginx landing (HTTP, DSM-managed).
- `https://${NAS_HOST}.${TAILNET_DOMAIN}/` → DSM nginx landing (HTTPS, DSM cert).
- `http://${NAS_HOST}.${TAILNET_DOMAIN}:${IMMICH_PORT}/` → Immich (HTTP, LAN-style path).
- `${IMMICH_URL}` → Immich (HTTPS, LE cert via Tailscale).

Inspect / undo:

```sh
sudo /usr/local/bin/tailscale serve status
sudo /usr/local/bin/tailscale serve --https=${IMMICH_PORT} off     # revert Immich HTTPS only
```

Expected Immich cert: CN `${NAS_HOST}.${TAILNET_DOMAIN}`, issuer Let's Encrypt,
auto-renewed by `tailscaled`. MagicDNS must be enabled on the tailnet for the
hostname to resolve.

To add HTTPS for another service later, same pattern:
```sh
sudo /usr/local/bin/tailscale serve --bg --https=<service-port> http://127.0.0.1:<service-port>
```

Tailnet peers we care about:
- `${NAS_HOST}` (this NAS, Linux) — always on.
- `${MAC_HOST}` (the MacBook / ML box) — our Phase Y compute node.

## Postgres exposed on tailnet (Phase Y)

2026-04-19: published `immich_postgres` on the NAS at host port **`${PG_PORT}`**
(container port 5432 internally). Bound to `0.0.0.0:${PG_PORT}` so the
tailnet IP `${PG_HOST}:${PG_PORT}` is reachable from Mac without
Tailscale Serve's TLS wrap (Synology's `tailscaled` runs in **userspace
networking mode** — `TUN: false` — so Docker's userland proxy can't
bind directly to the tailnet IP).

Why port **`${PG_PORT}`** and not 5432: DSM's own Postgres (Note Station etc.)
already owns `5432` on the NAS host.

Compose fragment:
```yaml
# docker-compose.yml, service: database
ports:
  - "${PG_PORT}:5432"
```

Connect from Mac:
```sh
PGPASSWORD=<DB_PASSWORD from .env> psql -h "${PG_HOST}" -p "${PG_PORT}" \
  -U postgres -d immich -c 'SELECT count(*) FROM asset;'
```

**Security note**: the port is also reachable on LAN (bound to
`0.0.0.0`, not tailnet-only). For our home NAS behind NAT with a strong
DB password this is acceptable for now. To harden later: add a DSM
Firewall rule (`Control Panel → Security → Firewall`) allowing `${PG_PORT}`
only from `100.64.0.0/10` (tailnet range) and blocking from all other
sources. Track as a Phase Y follow-up — not urgent.

Rollback (if needed): edit compose to remove the `ports:` block on
`database`, `docker compose up -d`. PG goes back to docker-network-only.
Backup of pre-change compose lives at
`${DOCKER_ROOT}/docker-compose.yml.bak-<timestamp>`.

## Backup

Not automated yet. Until Hyper Backup is wired up, the minimum manual drill
before anything risky (version upgrade, schema change, disk shuffle):

```sh
mkdir -p "${BACKUP_ROOT}"
cd "${DOCKER_ROOT}"
sudo -n /usr/local/bin/docker compose exec -T database \
  pg_dumpall --clean --if-exists -U postgres \
  | gzip > "${BACKUP_ROOT}"/immich-$(date +%F).sql.gz
gunzip -t "${BACKUP_ROOT}"/immich-*.sql.gz && echo "dump OK"
tar -C "${DEPLOY_ROOT}" -czf \
  "${BACKUP_ROOT}"/library-$(date +%F).tar.gz library
```

`sudo` is required on DSM — the `${DSM_USER}` user is not in the docker group,
and without it `docker compose exec` dies with `permission denied while
trying to connect to the Docker daemon socket` but the shell pipeline
still produces a valid-looking 20-byte empty `.sql.gz`. **Always run
`gunzip -t` after** to catch that silent fail.

First-run drill on 2026-04-19 produced `immich-*.sql.gz` = 16 MB and
`library-*.tar.gz` = 92 MB (an empty External Library + one test upload
from the iOS round-trip). Copy both off-NAS (external drive, C2, etc.)
after each run — files on the same volume aren't a backup.

## What Phase 0 deliberately did **not** install

- No DSM reverse proxy / Let's Encrypt cert — not needed while Tailscale is
  the only ingress.
- No separate `/volumeNVMe` pool — the NVMe is cache, and that's fine for now.
- No Mac ML node, no sidecar — that's Phase 1 / Phase 2.
- No external library contents — the folder is empty, waiting for the Phase 2
  ingest funnel to write into it.
