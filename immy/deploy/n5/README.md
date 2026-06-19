# immy on the N5 (Phase 6)

Run immy's enrichment **on the NAS** as a one-shot container that joins
Immich's docker network — so it can reach the ML container's internal-only
port (`immich_machine_learning:3003`) for CLIP, plus Postgres by the alias
`database`, and Ollama / the qwen-asr shim by container name (once they're
attached to the same network — see setup).

immy here is a **thin HTTP orchestrator**: no GPU, no weights, no ONNX/MLX in
the image. CLIP → Immich ML, captions → Ollama/gemma4, ASR → qwen-asr shim.

## Why this shape (and not the alternatives)

- **Not folded into the TrueNAS `ix-immich` stack** — TrueNAS regenerates that
  compose and would clobber edits.
- **Not a TrueNAS "custom app"** — re-introduces the managed-layer that can
  interfere on upgrades; buys only UI visibility/autostart, which a batch job
  doesn't need.
- **A standalone compose project invoked via `docker compose run --rm`** — joins
  `ix-immich_default` (external), gets container-name access to ML + PG, and
  leaves no idle daemon. Visibility comes from this dir being in git.

## One-time setup on the NAS

```sh
ssh n5
sudo mkdir -p /mnt/flash/immy/scratch

# Sync the package (incl. deploy/n5) to the NAS. Keep the deploy/n5 layout so
# the compose file's `build.context: ../..` resolves to the package dir. Exclude
# the gitignored MLX weights (mlx-community/, ~578M) — not needed in the image.
# From the repo root:
#   rsync -a --delete --exclude .venv --exclude .git --exclude tests \
#     --exclude mlx-community --exclude '__pycache__' --exclude uv.lock \
#     immy/ n5:/mnt/flash/immy/src-immy/
#   cp /mnt/flash/immy/src-immy/deploy/n5/config.example.yml /mnt/flash/immy/config.yml  # then edit
#
# Use this compose file for every command below:
#   CF=/mnt/flash/immy/src-immy/deploy/n5/compose.yaml

# Fill in /mnt/flash/immy/config.yml:
#  - pg.password   = immich_postgres POSTGRES_PASSWORD
#  - immich.api_key + immich.library_id = same as your Mac config's immich:
#    block (process needs library_id to anchor originalPath; url stays the
#    internal immich_server:2283).
# This file is intentionally NOT in git.

# Attach Ollama + the qwen-asr shim to Immich's network so immy can reach them
# by container name. (The host.docker.internal/host-gateway path is unreliable:
# the published-port hairpin NAT resets HTTP responses back to a container on
# ix-immich_default — TCP connects, HTTP gets reset.) This is additive and
# reversible (`docker network disconnect`), but must be re-run if those
# containers are recreated (reboot/update) unless their run command adds the net.
sudo docker network connect ix-immich_default ollama
sudo docker network connect ix-immich_default qwen-asr-shim

# Build the image (compose build.context points at the package dir):
sudo docker compose -f $CF build
```

## Smoke test (no DB writes)

```sh
CF=/mnt/flash/immy/src-immy/deploy/n5/compose.yaml

# 1. image runs + immy imports cleanly
sudo docker compose -f $CF run --rm immy --help

# 2. reachability from inside the network — all four by CONTAINER NAME
sudo docker compose -f $CF run --rm --entrypoint sh immy -c '
  python3 -c "import urllib.request as u; print(\"ML  :\", u.urlopen(\"http://immich_machine_learning:3003/ping\", timeout=8).read())" ;
  python3 -c "import socket; s=socket.create_connection((\"database\",5432),8); print(\"PG  : ok\"); s.close()" ;
  python3 -c "import urllib.request as u; print(\"OLLM:\", u.urlopen(\"http://ollama:11434/api/version\", timeout=8).read())" ;
  python3 -c "import urllib.request as u; print(\"ASR :\", u.urlopen(\"http://qwen-asr-shim:8091/ping\", timeout=8).read())"
'

# 3. dry-run a real trip — reports would-insert rows, writes nothing
/mnt/flash/immy/run-batch.sh /originals/<trip> --dry-run
```

> **Not yet safe for a real (non-dry-run) write.** Reviewed 2026-06-19 (codex+grok):
> originals are mounted `:ro` but `process` writes `.audit/` (journal, heartbeat,
> marker, staged derivatives, `.srt`) next to the trip, and `process` alone does
> not publish `asset_file` rows. A real run needs the write-path decisions in
> raw/IMMY-ON-N5.md resolved first.

## Real runs

```sh
# CLIP + derivatives (faces forced off — see run-batch.sh):
./run-batch.sh /originals/<trip>

# add captions + transcripts:
./run-batch.sh /originals/<trip> --with-captions --with-transcripts
```

## First-run verification (do once)

- **originalPath anchoring** — confirm the `--dry-run` output's would-insert
  `originalPath` matches what an Immich library scan produces (anchored to the
  library `importPaths`, which immy reads from the DB). A prefix mismatch means
  the originals mount or the library import path is off.
- **CLIP dim guard** — immy checks the embedding width against
  `smart_search.embedding` typmod up front; a `clip_model` mismatch fails fast.

## Cron (optional)

`docker compose run --rm` is cron-friendly. Example — nightly enrichment of a
fixed trip root, captions+transcripts on:

```cron
30 2 * * *  /mnt/flash/immy/run-batch.sh /originals/incoming --with-captions --with-transcripts >> /mnt/flash/immy/cron.log 2>&1
```
