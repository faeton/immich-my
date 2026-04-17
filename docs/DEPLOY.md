# Deploy — Phase 0 as built

What's actually running today on the DS923+ (`vv`). Update this file when the
deployment changes; it's the single source of truth for "where do things live
and how do I restart them".

## Host

- Synology DS923+, DSM 7.2+, Container Manager installed.
- NVMe slots: **SSD read/write cache** (`md3`, RAID1) in front of `/volume1`.
  Not a separate storage pool. All Immich state lives on `/volume1` and gets
  the cache's benefit transparently.
- Access: **Tailscale-first**. The DS923+ runs the Synology Tailscale package
  (tailnet `example.ts.net`, host `nas-media`). **Principle: every service keeps
  its native port; Tailscale Serve adds HTTPS in parallel on the same port.**
  DSM's landing on `:80`/`:443` is untouched. Canonical Immich URL:
  **`https://nas-media.example.ts.net:2283/`** — valid Let's Encrypt cert
  auto-renewed by `tailscaled`. LAN access on `http://<lan-ip>:2283/` still
  works unchanged (tailscale serve intercepts on the tailnet IP only).
- User: `faeton`. This deployment is deliberately separate from `saicheg`'s
  existing containers — nothing shared, nothing co-named.

## Filesystem layout

Dedicated top-level share (not under the user's home dir, so DSM User Home
Service changes can't nuke it):

```
/volume1/faeton-immi/
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

- Project name: **`fnim`** (short, clearly mine, won't collide with anything
  `saicheg` runs).
- Container names: `immich_server`, `immich_postgres`, `immich_redis`,
  `immich_machine_learning`.
- Compose file: patched from the Immich release `docker-compose.yml`. Diffs vs
  upstream:
  - `name: immich` → `name: fnim`
  - `immich-server` volumes: added
    `- /volume1/faeton-immi/originals:/mnt/external/originals:ro`
  - `immich-machine-learning` model cache: bind mount
    `- /volume1/faeton-immi/docker/model-cache:/cache` (replaces the named
    volume) — keeps model blobs on the NAS share, not in the Docker root.
  - Top-level `volumes: model-cache:` block removed (unused after the bind).
  - `DB_STORAGE_TYPE: 'HDD'` left commented — /volume1 is SSD-cached btrfs.

### `.env` (do not commit secrets anywhere shared)

```env
UPLOAD_LOCATION=/volume1/faeton-immi/library
DB_DATA_LOCATION=/volume1/faeton-immi/docker/postgres
TZ=Europe/Lisbon
IMMICH_VERSION=release
DB_PASSWORD=***                   # regenerate if this file ever leaks
DB_USERNAME=postgres
DB_DATABASE_NAME=immich
```

Container-internal paths the rest of the plan references:
- `/data` — server's view of `UPLOAD_LOCATION`.
- `/mnt/external/originals` — read-only view of the host `originals/` share;
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
cd /volume1/faeton-immi/docker

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

(Container Manager's UI shows the same project as `fnim` and can start/stop
it, but the CLI is faster for logs.)

## Tailscale Serve (HTTPS on native ports)

Model: DSM keeps `:80` and `:443` for its own landing / Application Portal
(unchanged). We do **not** shadow 443. Instead, each container we want exposed
gets an HTTPS wrapper on its own port via Tailscale Serve.

Current serve config:

```sh
sudo /usr/local/bin/tailscale serve --bg --https=2283 http://127.0.0.1:2283
```

Result over tailnet:
- `http://nas-media.example.ts.net/` → DSM nginx landing (HTTP, DSM-managed).
- `https://nas-media.example.ts.net/` → DSM nginx landing (HTTPS, DSM cert).
- `http://nas-media.example.ts.net:2283/` → Immich (HTTP, LAN-style path).
- `https://nas-media.example.ts.net:2283/` → Immich (HTTPS, LE cert via Tailscale).

Inspect / undo:

```sh
sudo /usr/local/bin/tailscale serve status
sudo /usr/local/bin/tailscale serve --https=2283 off     # revert Immich HTTPS only
```

Expected Immich cert: CN `nas-media.example.ts.net`, issuer Let's Encrypt,
auto-renewed by `tailscaled`. MagicDNS must be enabled on the tailnet for the
hostname to resolve.

To add HTTPS for another service later, same pattern:
```sh
sudo /usr/local/bin/tailscale serve --bg --https=<service-port> http://127.0.0.1:<service-port>
```

Tailnet peers we care about:
- `nas-media` (this NAS, Linux) — always on.
- `mac-ml` (the MacBook) — our Phase 1 ML box.

## Backup

Not automated yet. Until Hyper Backup is wired up, the minimum manual drill
before anything risky (version upgrade, schema change, disk shuffle):

```sh
cd /volume1/faeton-immi/docker
/usr/local/bin/docker compose exec -T database \
  pg_dumpall --clean --if-exists -U postgres \
  | gzip > /volume1/faeton-immi/backup/immich-$(date +%F).sql.gz
tar -C /volume1/faeton-immi -czf \
  /volume1/faeton-immi/backup/library-$(date +%F).tar.gz library
```

(Create `/volume1/faeton-immi/backup/` first and point it at external storage.)

## What Phase 0 deliberately did **not** install

- No DSM reverse proxy / Let's Encrypt cert — not needed while Tailscale is
  the only ingress.
- No separate `/volumeNVMe` pool — the NVMe is cache, and that's fine for now.
- No Mac ML node, no sidecar — that's Phase 1 / Phase 2.
- No external library contents — the folder is empty, waiting for the Phase 2
  ingest funnel to write into it.
